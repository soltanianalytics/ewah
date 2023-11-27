from ewah.constants import EWAHConstants as EC
from ewah.hooks.google_analytics import EWAHGoogleAnalyticsHook
from ewah.operators.base import EWAHBaseOperator

import json
from datetime import datetime, timedelta


class EWAHGAOperator(EWAHBaseOperator):
    _NAMES = ["ga", "google_analytics"]

    _ACCEPTED_EXTRACT_STRATEGIES = {
        EC.ES_FULL_REFRESH: False,
        EC.ES_INCREMENTAL: True,
        EC.ES_SUBSEQUENT: True,
    }

    _CONN_TYPE = EWAHGoogleAnalyticsHook.conn_type

    def __init__(
        self,
        view_id,
        dimensions,
        metrics,
        page_size=10000,
        include_empty_rows=True,
        sampling_level=None,
        *args,
        **kwargs
    ):
        if kwargs.get("primary_key"):
            raise Exception(
                "primary_key supplied, but the field is "
                + "auto-generated by the operator!"
            )

        # Set default to otherwise non-defaulted kwargs
        if not kwargs.get("load_data_from_relative"):
            kwargs["load_data_from_relative"] = timedelta(days=3)

        shorthand = "ga:"
        dimensions = [
            ("" if dim.startswith(shorthand) else shorthand) + dim for dim in dimensions
        ]
        metrics = [
            ("" if metric.startswith(shorthand) else shorthand) + metric
            for metric in metrics
        ]

        if kwargs.get("extract_strategy") == EC.ES_SUBSEQUENT:
            if "ga:dateHour" in dimensions:
                kwargs["subsequent_field"] = "dateHour"
            else:
                kwargs["subsequent_field"] = "date"

        kwargs.update({"primary_key": [dim[3:] for dim in dimensions]})

        self.view_id = view_id
        self.sampling_level = sampling_level
        self.dimensions = dimensions
        self.metrics = metrics
        self.page_size = page_size
        self.include_empty_rows = include_empty_rows

        super().__init__(*args, **kwargs)

        if not any(["ga:dateHour" in dimensions, "ga:date" in dimensions]):
            raise Exception("'date' or 'dateHour' must be a dimension!")

        if len(dimensions) > 7:
            raise Exception(
                (
                    "Can only fetch up to 7 dimensions!" + " Currently {0} Dimensions"
                ).format(
                    str(len(dimensions)),
                )
            )

        if len(metrics) > 10:
            raise Exception(
                (
                    "Can only fetch up to 10 metrics!" + " Currently {0} Dimensions"
                ).format(
                    str(len(metrics)),
                )
            )

        if self.page_size > 10000:
            raise Exception("Please specify a page size equal to or lower than 10000.")

    def ewah_execute(self, context):
        if (
            self.extract_strategy == EC.ES_SUBSEQUENT
            and self.test_if_target_table_exists()
        ):
            data_from = self.get_max_value_of_column(self.subsequent_field)
            if self.load_data_from_relative:
                data_from -= self.load_data_from_relative
        else:
            data_from = self.data_from

        if isinstance(data_from, datetime):
            # This is not the case on subsequent loads with 'date' as time dimension
            data_from = data_from.date()

        for batch in self.source_hook.get_data_in_batches(
            view_id=self.view_id,
            dimensions=self.dimensions,
            metrics=self.metrics,
            page_size=self.page_size,
            include_empty_rows=self.include_empty_rows,
            sampling_level=self.sampling_level,
            data_from=data_from,  # tbd: subsequent!
            data_until=(self.data_until or datetime.now()).date(),
            chunking_interval=self.load_data_chunking_timedelta
            or timedelta(days=7 * 13),
        ):
            self.upload_data(batch)
