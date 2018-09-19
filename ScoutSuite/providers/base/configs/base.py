# -*- coding: utf-8 -*-

import copy

from threading import Thread

# Python2 vs Python3
try:
    from Queue import Queue
except ImportError:
    from queue import Queue

from hashlib import sha1

from ScoutSuite.providers.base.configs.threads import thread_configs

# TODO do this better without name conflict
from opinel.utils.aws import connect_service
from ScoutSuite.providers.gcp.utils import gcp_connect_service

from opinel.utils.aws import build_region_list, handle_truncated_response
from opinel.utils.console import printException, printInfo

from ScoutSuite.output.console import FetchStatusLogger
from ScoutSuite.utils import format_service_name

# TODO global is trash
status = None
formatted_string = None


class BaseConfig():

    def __init__(self, thread_config=4, **kwargs):
        """

        :param thread_config:
        """

        self.library_type = None if not hasattr(self, 'library_type') else self.library_type

        self.service = type(self).__name__.replace('Config','').lower()  # TODO: use regex with EOS instead of plain replace
        self.thread_config = thread_configs[thread_config]

    def _is_provider(self, provider_name):
        return False

    def get_non_provider_id(self, name):
        """
        Not all AWS resources have an ID and some services allow the use of "." in names, which break's Scout2's
        recursion scheme if name is used as an ID. Use SHA1(name) instead.

        :param name:                    Name of the resource to
        :return:                        SHA1(name)
        """
        m = sha1()
        m.update(name.encode('utf-8'))
        return m.hexdigest()

    def fetch_all(self, credentials, regions=[], partition_name='aws', targets=None):
        """
        :param credentials:             F
        :param service:                 Name of the service
        :param regions:                 Name of regions to fetch data from
        :param partition_name:          AWS partition to connect to
        :param targets:                 Type of resources to be fetched; defaults to all.
        :return:
        """
        global status, formatted_string

        # Initialize targets
        if not targets:
            targets = type(self).targets
        printInfo('Fetching %s config...' % format_service_name(self.service))
        formatted_string = None

        # Connect to the service
        if self._is_provider('aws'):
            if self.service in ['s3']:  # S3 namespace is global but APIs aren't....
                api_clients = {}
                for region in build_region_list(self.service, regions, partition_name):
                    api_clients[region] = connect_service('s3', credentials, region, silent=True)
                api_client = api_clients[list(api_clients.keys())[0]]
            elif self.service == 'route53domains':
                api_client = connect_service(self.service, credentials, 'us-east-1',
                                             silent=True)  # TODO: use partition's default region
            else:
                api_client = connect_service(self.service, credentials, silent=True)

        elif self._is_provider('gcp'):
            api_client = gcp_connect_service(service=self.service, credentials=credentials)

        # Threading to fetch & parse resources (queue consumer)
        params = {'api_client': api_client}

        if self._is_provider('aws'):
            if self.service in ['s3']:
                params['api_clients'] = api_clients

        # Threading to parse resources (queue feeder)
        target_queue = self._init_threading(self.__fetch_target, params, self.thread_config['parse'])

        # Threading to list resources (queue feeder)
        params = {'api_client': api_client, 'q': target_queue}

        if self._is_provider('aws'):
            if self.service in ['s3']:
                params['api_clients'] = api_clients

        service_queue = self._init_threading(self.__fetch_service, params, self.thread_config['list'])

        # Init display
        self.fetchstatuslogger = FetchStatusLogger(targets)

        # Go
        for target in targets:
            service_queue.put(target)

        # Join
        service_queue.join()
        target_queue.join()

        if self._is_provider('aws'):
            # Show completion and force newline
            if self.service != 'iam':
                self.fetchstatuslogger.show(True)
        else:
            self.fetchstatuslogger.show(True)

    def __fetch_target(self, q, params):
        global status
        try:
            while True:
                try:
                    target_type, target = q.get()
                    # Make a full copy of the target in case we need to re-queue it
                    backup = copy.deepcopy(target)
                    method = getattr(self, 'parse_%s' % target_type)
                    method(target, params)
                    self.fetchstatuslogger.counts[target_type]['fetched'] += 1
                    self.fetchstatuslogger.show()
                except Exception as e:
                    if hasattr(e, 'response') and \
                            'Error' in e.response and \
                            e.response['Error']['Code'] in ['Throttling']:
                        q.put((target_type, backup), )
                    else:
                        printException(e)
                finally:
                    q.task_done()
        except Exception as e:
            printException(e)
            pass

    def __fetch_service(self, q, params):
        api_client = params['api_client']
        try:
            while True:
                try:
                    target_type, response_attribute, list_method_name, list_params, ignore_list_error = q.get()
                    if not list_method_name:
                        continue
                    try:

                        # This is a specific case for GCP services that don't have a native cloud library
                        if self.library_type == 'api_client_library':
                            target = getattr(api_client, target_type)
                            method = getattr(target(), list_method_name)
                        # This works for AWS and GCP cloud libraries
                        else:
                            method = getattr(api_client, list_method_name)

                    except Exception as e:
                        printException(e)
                        continue

                    try:

                        # TODO put this code in each provider
                        # should return the list of targets

                        # AWS provider
                        if self._is_provider('aws'):
                            if type(list_params) != list:
                                list_params = [list_params]
                            targets = []
                            for lp in list_params:
                                targets += handle_truncated_response(method, lp, [response_attribute])[
                                    response_attribute]

                        # GCP provider
                        elif self._is_provider('gcp'):
                            targets = []

                            # TODO this is temporary, will have to be moved to Config children objects
                            # What this does is create a list with all combinations of possibilities for method parameters
                            list_params_list = []
                            # only projects
                            if 'project' in list_params.keys() and not 'zone' in list_params.keys():
                                for project in self.projects:
                                    temp_list_params = dict(list_params)
                                    temp_list_params['project'] = project
                                    list_params_list.append(temp_list_params)
                            # only zones
                            elif not 'project' in list_params.keys() and 'zone' in list_params.keys():
                                zones = self.get_zones(client=api_client, project=self.projects[0])
                                for zone in zones:
                                    temp_list_params = dict(list_params)
                                    temp_list_params['zone'] = zone
                                    list_params_list.append(temp_list_params)
                            # projects and zones
                            elif 'project' in list_params.keys() and 'zone' in list_params.keys():
                                zones = self.get_zones(client=api_client, project=self.projects[0])
                                import itertools
                                for elem in list(itertools.product(*[self.projects, zones])):
                                    temp_list_params = dict(list_params)
                                    temp_list_params['project'] = elem[0]
                                    temp_list_params['zone'] = elem[1]
                                    list_params_list.append(temp_list_params)
                            # neither projects nor zones
                            else:
                                list_params_list.append(list_params)

                            for list_params_combination in list_params_list:

                                try:

                                    if self.library_type == 'cloud_client_library':
                                        response = method(**list_params_combination)
                                        targets += list(response)
                                        # Remove client as it's unpickleable and adding the object to the Queue will pickle
                                        # The client is later re-inserted in each Config
                                        for t in targets:
                                            t._client = None

                                    if self.library_type == 'api_client_library':

                                        response = method(**list_params_combination).execute()
                                        if 'items' in response:
                                            targets += response['items']

                                        # TODO need to handle too long responses
                                        # request = method(**list_params)
                                        # while request is not None:
                                        #     response = request.execute()
                                        #     if 'items' in response:
                                        #         targets += response['items']
                                        #     try:
                                        #         request = api_entity.list_next(previous_request=request,
                                        #                                        previous_response=response)
                                        #     except AttributeError:
                                        #         request = None

                                except Exception as e:
                                    if not ignore_list_error:
                                        printException(e)

                    except Exception as e:
                        if not ignore_list_error:
                            printException(e)
                        targets = []
                    self.fetchstatuslogger.counts[target_type]['discovered'] += len(targets)
                    for target in targets:
                        params['q'].put((target_type, target), )
                except Exception as e:
                    printException(e)
                finally:
                    q.task_done()
        except Exception as e:
            printException(e)
            pass

    def finalize(self):
        for t in self.fetchstatuslogger.counts:
            setattr(self, '%s_count' % t, self.fetchstatuslogger.counts[t]['fetched'])
        self.__delattr__('fetchstatuslogger')

    def _init_threading(self, function, params={}, num_threads=10):
        # Init queue and threads
        q = Queue(maxsize=0)  # TODO: find something appropriate
        for i in range(num_threads):
            worker = Thread(target=function, args=(q, params))
            worker.setDaemon(True)
            worker.start()
        return q