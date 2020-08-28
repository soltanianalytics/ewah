"""
Modified version of the google analytics to s3 operator
Source of original: https://github.com/airflow-plugins/google_analytics_plugin/blob/master/operators/google_analytics_reporting_to_s3_operator.py
Accessed 5 March 2019
"""
from ewah.operators.base_operator import EWAHBaseOperator
from ewah.ewah_utils.airflow_utils import airflow_datetime_adjustments
from ewah.constants import EWAHConstants as EC

from airflow.hooks.base_hook import BaseHook

import json
import time
from datetime import timedelta
from apiclient.discovery import build
from oauth2client.service_account import ServiceAccountCredentials as SAC


class EWAHGAOperator(EWAHBaseOperator):

    template_fields = ('data_from', 'data_until')

    _IS_INCREMENTAL = True
    _IS_FULL_REFRESH = False

    _API_CORE_V3 = 'core_v3'
    _API_CORE_V4 = 'core_v4'
    _API_MULTI = 'multichannel'

    _ACCEPTED_API = [
        # _API_CORE_V3,
        _API_CORE_V4,
        # _API_MULTI,
    ]

    def __init__(
        self,
        api, # one of _API_CORE_V3, _API_CORE_V4, _API_MULTI
        view_id,
        dimensions,
        metrics,
        data_from=None,
        data_until=None,
        page_size=10000,
        include_empty_rows=True,
        sampling_level=None,
        chunking_interval=None, # must be timedelta
        reload_data_from=None, # If a new table is added in production, and
        #   it is loading incrementally, where to start loading data? datetime
        reload_data_chunking=None, # must be timedelta
        # Note: Need chunking despite pagination as DWH upload must also be
        #   chunked, not just data loading, especially for reloads / backfills!
    *args, **kwargs):
        if kwargs.get('update_on_columns'):
            raise Exception('update_on_columns supplied, but the field is ' \
                + 'auto-generated by the operator!')
        if reload_data_from and not (reload_data_chunking or chunking_interval):
            raise Exception('When setting reload_data_from, must also set ' \
                + 'either reload_data_chunking or chunking_interval!')

        if not api in self._ACCEPTED_API:
            raise Exception('api must be one of these: {0}'.format(
                ', '.join(self._ACCEPTED_API),
            ))

        if api == self._API_MULTI:
            shorthand = 'mcf:'
        else:
            shorthand = 'ga:'

        dimensions = [
            ('' if dim.startswith(shorthand) else shorthand) + dim
            for dim in dimensions
        ]
        metrics = [
            ('' if metric.startswith(shorthand) else shorthand) + metric
            for metric in metrics
        ]

        kwargs.update({'update_on_columns': [dim[3:] for dim in dimensions]})

        self.credentials = BaseHook.get_connection(kwargs['source_conn_id']) # dont commit this
        self.credentials = self.credentials.extra_dejson
        self.api = api
        self.view_id = view_id
        self.data_from = data_from
        self.data_until = data_until
        self.sampling_level = sampling_level
        self.dimensions = dimensions
        self.metrics = metrics
        self.page_size = page_size
        self.include_empty_rows = include_empty_rows
        self.chunking_interval = chunking_interval
        self.reload_data_from = reload_data_from
        self.reload_data_chunking = reload_data_chunking or chunking_interval

        self.metricMap = {
            'METRIC_TYPE_UNSPECIFIED': 'varchar(255)',
            'CURRENCY': 'decimal(20,5)',
            'INTEGER': 'int(11)',
            'FLOAT': 'decimal(20,5)',
            'PERCENT': 'decimal(20,5)',
            'TIME': 'time'
        }

        super().__init__(*args, **kwargs)

        if chunking_interval and not (type(chunking_interval) == timedelta):
            raise Exception('If supplied, chunking_interval must be timedelta!')

        if not self.credentials.get('client_secrets'):
            raise Exception('Google Analytics Credentials misspecified!' \
                + ' Example of a correct specifidation: {0}'.format(
                    json.dumps({"client_secrets":{
                        "type": "service_account",
                        "project_id": "abc-123",
                        "private_key_id": "123456abcder",
                        "private_key": "-----BEGIN PRIVATE KEY-----\nxxx\n-----END PRIVATE KEY-----\n",
                        "client_email": "xyz@abc-123.iam.gserviceaccount.com",
                        "client_id": "123457",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/xyz%40abc-123.iam.gserviceaccount.com"
                    }})
                ))

        if len(dimensions) > 7:
            raise Exception(('Can only fetch up to 7 dimensions!' \
                + ' Currently {0} Dimensions').format(
                    str(len(dimensions)),
            ))

        if len(metrics) > 10:
            raise Exception(('Can only fetch up to 10 metrics!' \
                + ' Currently {0} Dimensions').format(
                    str(len(metrics)),
            ))

        if self.page_size > 10000:
            raise Exception(
                'Please specify a page size equal to or lower than 10000.')


    def ewah_execute(self, context):
        self.data_until = airflow_datetime_adjustments(self.data_until)
        self.data_from = airflow_datetime_adjustments(self.data_from)
        self.reload_data_from = \
            airflow_datetime_adjustments(self.reload_data_from)

        self.data_until = self.data_until or context['next_execution_date']
        self.data_from = self.data_from or context['execution_date']

        if self.drop_and_replace or (not self.test_if_target_table_exists()):
            self.chunking_interval = self.reload_data_chunking \
                or self.chunking_interval \
                or (self.data_until - self.data_from)
            self.data_from = self.reload_data_from or context['dag'].start_date
            self.data_from = airflow_datetime_adjustments(self.data_from)
        elif not self.chunking_interval:
            self.chunking_interval = self.data_until - self.data_from

        self.log.info('Connecting to Google...')
        if self.api == self._API_CORE_V3:
            raise Exception('Not yet implemented')
        elif self.api == self._API_CORE_V4:
            kwargs = {
                'service_object': build(
                    'analyticsreporting',
                    'v4',
                    credentials=SAC.from_json_keyfile_dict(
                        self.credentials['client_secrets'],
                        ['https://www.googleapis.com/auth/analytics.readonly'],
                    )
                ),
                'context': context,
            }
            data_func = self.get_data_v4
        elif self.api == self._API_MULTI:
            raise Exception('Not yet implemented!')
            # Open: how to parse raw data to uploadable format? See get_data_multi
            kwargs = {
                'service_object': build(
                    'analytics',
                    'v3',
                    credentials=SAC.from_json_keyfile_dict(
                        self.credentials['client_secrets'],
                        ['https://www.googleapis.com/auth/analytics.readonly'],
                    )
                ),
                'context': context,
            }
            data_func = self.get_data_multi
        else:
            raise Exception('Not yet implemented!')

        i = 0
        while self.data_from < self.data_until:
            i += 1

            self.since_formatted = self.data_from.strftime('%Y-%m-%d')
            self.until_formatted = min(
                    self.data_from + self.chunking_interval,
                    self.data_until,
                ).strftime('%Y-%m-%d')
            self.log.info('Chunk {0}: From {1} through {2}'.format(
                str(i),
                self.since_formatted,
                self.until_formatted,
            ))

            self.upload_data(data_func(**kwargs))
            self.data_from += self.chunking_interval

    def get_data_multi(self, service_object, context):
        start_index = 1
        payload = {
            'ids': 'ga:' + self.view_id,
            'start_date': self.since_formatted,
            'end_date': self.until_formatted,
            'metrics': ','.join(self.metrics),
            'dimensions': ','.join(self.dimensions),
            'sort': ','.join(self.dimensions),
            # 'filters': not implemented!
            # 'samplingLevel': Default!
            'start_index': start_index,
            'max_results': self.page_size,
        }

        self.log.info('Getting data from page 1...')
        i = 1
        response = service_object.data().mcf().get(**payload).execute()
        data = response.get('rows', [])
        while response.get('nextLink'):
            i += 1
            self.log.info('Getting data from page {0}...'.format(str(i)))
            start_index += self.page_size
            payload.update({'start_index': start_index})
            response = service_object.data().mcf().get(**payload).execute()
            data += response,get('rows', [])

        uploadable_data = [] # TBU

        return uploadable_data

    def get_data_v4(self, service_object, context):
        reportRequest = {
            'viewId': self.view_id,
            'dateRanges': [{
                'startDate': self.since_formatted,
                'endDate': self.until_formatted,
            }],
            'samplingLevel': self.sampling_level,
            'dimensions': [{'name':d} for d in self.dimensions],
            'metrics': [{'expression':m} for m in self.metrics],
            'pageSize': self.page_size,
            'includeEmptyRows': self.include_empty_rows,
        }

        response = (service_object
                    .reports()
                    .batchGet(body={'reportRequests':[reportRequest]})
                    .execute())

        if response.get('reports'):
            report = response['reports'][0]
            rows = report.get('data', {}).get('rows', [])

            while report.get('nextPageToken'):
                time.sleep(1)
                reportRequest.update({'pageToken': report['nextPageToken']})
                response = (service_object
                            .reports()
                            .batchGet(body={'reportRequests':[reportRequest]})
                            .execute())
                report = response['reports'][0]
                rows.extend(report.get('data', {}).get('rows', []))

            if report['data']:
                report['data']['rows'] = rows
        else:
            report = {}

        columnHeader = report.get('columnHeader', {})
        # Right now all dimensions are hardcoded to varchar(255), will need a
        # map if any non-varchar dimensions are used in the future
        # Unfortunately the API does not send back types for Dimensions like it
        # does for Metrics (yet..)
        dimensionHeaders = [
            {
                'name': header.replace('ga:', ''),
                'type': 'varchar(255)',
            }
            for header
            in columnHeader.get('dimensions', [])
        ]
        metricHeaders = [
            {
                'name': entry.get('name').replace('ga:', ''),
                'type': self.metricMap.get(entry.get('type'), 'varchar(255)'),
            }
            for entry in
            columnHeader.get('metricHeader', {}).get('metricHeaderEntries', [])
        ]

        uploadable_data = []
        rows = report.get('data', {}).get('rows', [])
        for row_counter, row in enumerate(rows):
            root_data_obj = {}
            dimensions = row.get('dimensions', [])
            metrics = row.get('metrics', [])

            for index, dimension in enumerate(dimensions):
                header = dimensionHeaders[index].get('name') # .lower()
                root_data_obj[header] = dimension

            for metric in metrics:
                data = {}
                data.update(root_data_obj)

                for index, value in enumerate(metric.get('values', [])):
                    header = metricHeaders[index].get('name') # .lower()
                    data[header] = value

                data['view_id'] = self.view_id

                uploadable_data += [data]

        return uploadable_data
