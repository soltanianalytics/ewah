from ewah.constants import EWAHConstants as EC
from ewah.hooks.base import EWAHBaseHook
from ewah.uploaders.base import EWAHBaseUploader

from airflow.operators.python import PythonOperator
from airflow.models import BaseOperator

from google.cloud.bigquery.table import TableReference
from google.cloud.bigquery import (
    Table,
    SchemaField,
    LoadJobConfig,
    CopyJobConfig,
)

from copy import deepcopy
from time import sleep
from tempfile import TemporaryFile, TemporaryDirectory
from datetime import datetime, date, timedelta


class BigqueryOperator(BaseOperator):
    "Operate to execute SQL on BigQuery"

    def __init__(
        self, sql, bigquery_conn_id, project=None, params=None, *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.sql = sql
        self.conn_id = bigquery_conn_id
        self.project = project
        # an attribute called "params" of an operator leads to an airflow error
        # if that attribute is None due to default templating of "task.params"!
        self._params = params

    def execute(self, context):
        from ewah.hooks.bigquery import EWAHBigQueryHook

        hook = EWAHBigQueryHook(
            conn_id=self.conn_id,
            project_id=self.project,
        )
        hook.execute(self.sql, commit=True, params=self._params)
        hook.commit()
        hook.close()


class FakeDatasetRef:
    def __init__(self, dataset_id, project_id):
        self.dataset_id = dataset_id
        self.project = project_id


class EWAHBigQueryUploader(EWAHBaseUploader):

    _QUERY_SCHEMA_CHANGES_COLUMNS = """
        SELECT column_name
        FROM `{project_id}.{schema_name}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = '{table_name}'
    """
    _QUERY_SCHEMA_CHANGES_ADD_COLUMN = """
        ALTER TABLE `{project_id}.{schema_name}.{table_name}`
        ADD COLUMN `{column_name}` {column_type};
    """

    _QUERY_TABLE = "SELECT * FROM `{project_id}.{schema_name}.{table_name}`"

    def __init__(
        self,
        *args,
        partition_field=None,
        partition_type=None,
        require_partition_filter=False,
        insert_chunk_size=100,
        **kwargs,
    ) -> None:
        super().__init__(EC.DWH_ENGINE_BIGQUERY, *args, **kwargs)
        # BigQuery project id aka database name may be set in connection
        self.database_name = self.database_name or self.dwh_hook.conn.project
        if partition_type or partition_field or require_partition_filter:
            assert (
                partition_type and partition_field
            ), "partition_type and partition_field must both be set if either is set or require_partition_filter is true!"
        self.partition_field = partition_field
        self.partition_type = partition_type
        self.insert_chunk_size = insert_chunk_size

    @classmethod
    def get_cleaner_callables(cls):
        def bigquery_data_adjustments(row):
            key_changes = []
            for key, value in row.items():
                # change date, datetime, timedelta types
                # reason: non-json-serializable types are not accepted
                if isinstance(value, (date, datetime)):
                    row[key] = value.isoformat()
                elif isinstance(value, timedelta):
                    row[key] = value.total_seconds()

                # prefix field names that start with a number because BigQuery
                # does not like those types of field names.
                # Sub-challenge: generally prefix all field names that are hex
                try:
                    int(key, 16)
                    # if that did not fail, it is a hex!
                    key_changes.append((key, "field_" + key))
                except ValueError:
                    # not a hex, does it start with a number still?
                    if key[:1].isdigit():
                        key_changes.append((key, "field_" + key))

            # execute key changes
            for old_key, new_key in key_changes:
                row[new_key] = row.pop(old_key)

            return row

        return [bigquery_data_adjustments]

    @classmethod
    def get_schema_tasks(
        cls,
        dag,
        dwh_engine,
        dwh_conn_id,
        target_schema_name,
        target_schema_suffix="_next",
        target_database_name=None,
        read_right_users=None,  # Only for PostgreSQL
        **additional_task_args,
    ):
        schema_full_path = ""
        if target_database_name:
            schema_full_path += target_database_name + "."
        schema_full_path += target_schema_name

        # Create functions that are subsequently used in a PythonOperator

        def kickoff_func(schema_full, schema_suffix, dwh_conn_id):
            # kickoff: create new dataset
            schema = schema_full + schema_suffix
            conn = EWAHBaseHook.get_hook_from_conn_id(dwh_conn_id).dbconn
            # delete dataset first if it already exists
            print("Deleting the dataset {0} if it already exists.".format(schema))
            conn.delete_dataset(schema, delete_contents=True, not_found_ok=True)
            print("Creating the dataset {0}.".format(schema_full))
            conn.create_dataset(schema)
            print("Done!")

        def final_func(schema_name, schema_suffix, dwh_conn_id):
            # final: move new data into the final dataset
            conn = EWAHBaseHook.get_hook_from_conn_id(dwh_conn_id).dbconn
            # get dataset objects
            try:  # create final dataset if not exists
                ds_final = conn.get_dataset(schema_name)
            except:
                print("Creating dataset {0}".format(schema_name))
                ds_final = conn.create_dataset(schema_name)
            ds_temp = conn.get_dataset(schema_name + schema_suffix)

            # copy all tables from temp dataset to final dataset
            new_tables = conn.list_tables(ds_temp)
            new_table_ids = [table.table_id for table in conn.list_tables(ds_temp)]
            old_table_ids = [table.table_id for table in conn.list_tables(ds_final)]
            copy_jobs = []
            for table in new_tables:
                print(
                    "Copying table {0} from temp to final dataset".format(
                        table.table_id
                    )
                )
                try:
                    old_table = conn.get_table(
                        table=TableReference(
                            dataset_ref=ds_final, table_id=table.table_id
                        )
                    )
                    conn.delete_table(old_table)
                except:
                    # ignore failure, fails if old table does not exist to begin with
                    pass
                finally:
                    final_table = ds_final.table(table.table_id)
                    copy_jobs.append(conn.copy_table(table, final_table))

            # delete tables that don't exist in temp dataset from final dataset
            for table_id in old_table_ids:
                if not table_id in new_table_ids:
                    print("Deleting table {0}".format(table_id))
                    conn.delete_table(
                        conn.get_table(
                            TableReference(dataset_ref=ds_final, table_id=table_id)
                        )
                    )

            # make sure all copy jobs succeeded
            while copy_jobs:
                sleep(0.1)
                job = copy_jobs.pop(0)
                job.result()
                assert job.state in ("RUNNING", "DONE")
                if job.state == "RUNNING":
                    copy_jobs.append(job)
                else:
                    print(
                        "Successfully copied {0}".format(
                            job.__dict__["_properties"]["configuration"]["copy"][
                                "destinationTable"
                            ]["tableId"]
                        )
                    )

            # delete temp dataset
            print("Deleting temp dataset.")
            conn.delete_dataset(ds_temp, delete_contents=True, not_found_ok=False)

            print("Done.")

        task_1_args = deepcopy(additional_task_args)
        task_2_args = deepcopy(additional_task_args)
        task_1_args.update(
            {
                "task_id": "kickoff",
                "dag": dag,
                "python_callable": kickoff_func,
                "op_kwargs": {
                    "schema_full": schema_full_path,
                    "schema_suffix": target_schema_suffix,
                    "dwh_conn_id": dwh_conn_id,
                },
            }
        )
        task_2_args.update(
            {
                "task_id": "final",
                "dag": dag,
                "python_callable": final_func,
                "op_kwargs": {
                    "schema_name": target_schema_name,
                    "schema_suffix": target_schema_suffix,
                    "dwh_conn_id": dwh_conn_id,
                },
            }
        )

        return (PythonOperator(**task_1_args), PythonOperator(**task_2_args))

    def rollback(self):
        raise Exception("Rollback attempted - BigQuery knows no transactions!")

    def close(self):
        pass  # nothing to do

    def commit(self):
        pass  # nothing to do

    def test_if_table_exists(self, table_name, schema_name, project_id=None):

        if project_id:
            dataset_path = "{0}.{1}".format(project_id, schema_name)
        else:
            dataset_path = schema_name
        if not self.test_if_dataset_exists(dataset_path):
            return False
        return 0 < len(
            self.dwh_hook.execute_and_return_result(
                sql="""
                        SELECT * FROM `{dataset}.__TABLES__`
                        WHERE table_id = '{table_name}'
                    """.format(
                    dataset=dataset_path, table_name=table_name
                )
            )
        )

    def test_if_dataset_exists(self, dataset_id):
        conn = self.dwh_hook.dbconn
        try:
            ds = conn.get_dataset(dataset_id)
        except:
            # fails if dataset does not exist
            return False
        return True

    def get_max_value_of_column(self, column_name):
        return self.dwh_hook.execute_and_return_result(
            sql="SELECT MAX(`{0}`) FROM `{1}.{2}.{3}`".format(
                column_name,
                self.database_name,
                self.schema_name + self.schema_suffix,
                self.table_name,
            ),
            return_dict=False,
        )[0][0]

    def drop_table_if_exists(self, table_name, schema_name, project_id=None):
        if self.test_if_table_exists(
            table_name=table_name,
            schema_name=schema_name,
            project_id=project_id,
        ):
            conn = self.dwh_hook.dbconn
            conn.delete_table(
                conn.get_table(
                    TableReference(
                        dataset_ref=FakeDatasetRef(
                            dataset_id=schema_name,
                            project_id=project_id or self.database_name,
                        ),
                        table_id=table_name,
                    )
                )
            )

    def copy_table(self):
        # Overwrite parent method for alternative approach
        # If target table was created by a previous run, drop it first
        self.drop_table_if_exists(
            self.table_name, self.schema_name + self.schema_suffix, self.database_name
        )
        # Create target table if source table exists
        if self.test_if_table_exists(
            table_name=self.table_name,
            schema_name=self.schema_name,
            project_id=self.database_name,
        ):
            self.log.info("Copying table into temporary dataset!")
            conn = self.dwh_hook.dbconn
            ds_old = conn.get_dataset(self.schema_name)
            ds_new = conn.get_dataset(self.schema_name + self.schema_suffix)
            table_old = conn.get_table(
                table=TableReference(dataset_ref=ds_old, table_id=self.table_name)
            )
            table_new = ds_new.table(self.table_name)
            copy_job = conn.copy_table(
                table_old,
                table_new,
                job_config=CopyJobConfig(write_disposition="WRITE_TRUNCATE"),
            )
            sleep(1)

            while True:
                copy_job.result()
                assert copy_job.state in (
                    "RUNNING",
                    "DONE",
                ), "Unexpected job state: {0}".format(job.state)
                if copy_job.state == "DONE":
                    self.log.info(
                        "Successfully copied {0}".format(
                            copy_job.__dict__["_properties"]["configuration"]["copy"][
                                "destinationTable"
                            ]["tableId"]
                        )
                    )
                    break
                # Wait 5s, try again
                sleep(5)

    def _create_or_update_table(
        self,
        data,
        table_name,
        schema_name,
        schema_suffix,
        columns_definition,
        load_strategy,
        upload_call_count,
        database_name=None,
        primary_key=None,
    ):
        project_id = database_name or self.database_name
        conn = self.dwh_hook.dbconn
        new_schema_name = schema_name + schema_suffix
        ds_new = conn.get_dataset(new_schema_name)

        # ensure all fields exist, even if null
        # otherwise, rarely-populated fields will cause data loading failure
        upload_data = []
        while data:
            datum = data.pop(0)
            upload_data.append(
                {field: datum.get(field) for field in columns_definition.keys()}
            )

        # create table if it does not yet exist / drop if it needs dropping
        table_exists = self.test_if_table_exists(
            table_name=table_name,
            schema_name=new_schema_name,
            project_id=project_id,
        )

        schema_definition = [
            SchemaField(name=name, field_type=field["data_type"])
            for name, field in columns_definition.items()
        ]
        # Must not use autodetect because it may differ between two uploads, and
        # if temp table differs from destination table below in terms of schema,
        # the insert will fail without error, and hence the data will be incomplete.
        # This is pretty nuts but it is true.
        job_config = LoadJobConfig(autodetect=False, schema=schema_definition)

        if (load_strategy == EC.LS_INSERT_REPLACE and upload_call_count == 1) or (
            not table_exists
        ):
            if table_exists:
                # Drop table before re-creating it
                conn.delete_table(
                    conn.get_table(
                        TableReference(dataset_ref=ds_new, table_id=table_name)
                    )
                )
            # create it anew
            table_obj = Table(".".join([project_id, new_schema_name, table_name]))
            if self.partition_field:
                table_obj.time_partitioning = bigquery.TimePartitioning(
                    type_=self.partition_type,
                    field=self.partition_field,
                )
                if self.require_partition_filter:
                    table_obj.require_partition_filter = True
            job = conn.load_table_from_json(
                json_rows=upload_data, destination=table_obj, job_config=job_config
            )
            try:
                job.result()
            except:
                self.log.info("Errors occured - job errors: {0}".format(job.errors))
                raise
            assert job.state == "DONE", "Invalid job state: {0}".format(job.state)
        else:
            # table already exists, load data into the table
            table_string = ".".join([project_id, new_schema_name, table_name])
            self.log.info("Uploading data now into {0}...".format(table_string))
            table_obj = Table(table_string)
            while upload_data:
                # The insert_rows method doesn't like large sets of data.
                # Instead, loop over the upload_data and upload small chunks of it.
                loop_data = upload_data[: self.insert_chunk_size]
                failed_tries = 0

                try:
                    conn.insert_rows(
                        table=table_obj,
                        rows=loop_data,
                        selected_fields=schema_definition,
                    )
                    # Delete is on purpose not executed if the insert_rows fails
                    # -> try to upload the same set of data in that case
                    del upload_data[: self.insert_chunk_size]
                except:
                    # Sometimes, BigQuery needs a bit of time to "know" the table
                    # actually exists if it was very recently created... try a few
                    # times with a delay before raising an actual error.
                    if failed_tries >= 5:
                        raise
                    else:
                        self.log.info("There appears to have been an error...")
                        self.log.info("Trying again!")
                        failed_tries += 1
                        sleep(10 * failed_tries)
