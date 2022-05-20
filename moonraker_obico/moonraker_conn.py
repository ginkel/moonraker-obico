from typing import Optional, Dict, List, Tuple
from numbers import Number
import re
import requests  # type: ignore
import logging

from .wsconn import WSConn, ConnHandler
from .utils import  FlowTimeout, Event

_logger = logging.getLogger('obico.moonraker_conn')

class MoonrakerConn(ConnHandler):
    max_backoff_secs = 30
    flow_step_timeout_msecs = 2000
    ready_timeout_msecs = 60000

    class KlippyGone(Exception):
        pass

    def __init__(self, id, app_config, sentry, on_event):
        super().__init__(id, sentry, on_event)
        self._next_id: int = 0
        self.app_config: Config = app_config
        self.config: MoonrakerConfig = app_config.moonraker
        self.websocket_id: Optional[int] = None
        self.printer_objects: Optional[list] = None
        self.heaters: Optional[List[str]] = None

    def api_get(self, mr_method, timeout=5, raise_for_status=True, **params):
        url = f'{self.config.http_address()}/{mr_method.replace(".", "/")}'
        _logger.debug('GET {url}')

        headers = {'X-Api-Key': self.config.api_key} if self.config.api_key else {}
        resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
        )

        if raise_for_status:
            resp.raise_for_status()

        return resp.json().get('result')

    def api_post(self, mr_method, filename=None, fileobj=None, **post_params):
        url = f'{self.config.http_address()}/{mr_method.replace(".", "/")}'
        _logger.debug('POST {url}')

        headers = {'X-Api-Key': self.config.api_key} if self.config.api_key else {}
        files={'file': (filename, fileobj, 'application/octet-stream')} if filename and fileobj else None
        resp = requests.post(
            url,
            headers=headers,
            data=post_params,
            files=files,
        )
        resp.raise_for_status()
        return resp.json()

    def next_id(self) -> int:
        next_id = self._next_id = self._next_id + 1
        return next_id

    def push_event(self, event):
        if self.shutdown:
            _logger.debug(f'is shutdown, dropping event {event}')
            return False

        return super().push_event(event)

    def flow(self) -> None:
        self.timer.reset(None)
        self.ready = False
        self.websocket_id = None

        if self.conn:
            self.conn.close()

        if not self.config.api_key:
            _logger.warning('api key is unset, trying to fetch one')
            self.config.api_key = self.api_get('access/api_key')

        self.conn = WSConn(
            id=self.id,
            auth_header_fmt='X-Api-Key: {}',
            sentry=self.sentry,
            url=self.config.ws_url(),
            token=self.config.api_key,
            on_event=self.push_event,
            ignore_pattern=re.compile(r'"method": "notify_proc_stat_update"')
        )

        self.conn.start()
        _logger.debug('waiting for connection')
        self.wait_for(self._received_connected)

        _logger.debug('requesting websocket_id')
        self.request_websocket_id()
        self.wait_for(self._received_websocket_id)

        self.app_config.webcam.update_from_moonraker(self)

        while True:
            _logger.info('waiting for klipper ready')
            self.ready = False
            try:
                while True:
                    rid = self.request_printer_info()
                    try:
                        self.wait_for(
                            self._received_printer_ready(rid),
                            self.ready_timeout_msecs)
                        break
                    except FlowTimeout:
                        continue

                _logger.debug('requesting printer objects')
                self.request_printer_objects()
                self.wait_for(self._received_printer_objects)

                _logger.debug('requesting heaters')
                self.request_heaters()
                self.wait_for(self._received_heaters)

                _logger.debug('subscribing')
                sub_id = self.request_subscribe()
                self.wait_for(self._received_subscription(sub_id))

                _logger.debug('requesting last job')
                self.request_job_list(order='desc', limit=1)
                self.wait_for(self._received_last_job)

                self.set_ready()
                _logger.info('connection is ready')
                self.on_event(
                    Event(sender=self.id, name=f'{self.id}_ready', data={})
                )

                # forwarding events
                self.loop_forever(self.on_event)
            except self.KlippyGone:
                _logger.warning('klipper got disconnected')
                continue

    def _wait_for(self, event, process_fn, timeout_msecs):
        if (
            event.data.get('method') == 'notify_klippy_disconnected'
        ):
            self.on_event(Event(sender=self.id, name='klippy_gone', data={}))
            raise self.KlippyGone

        return super(MoonrakerConn, self)._wait_for(
            event, process_fn, timeout_msecs)

    def _received_connected(self, event):
        if event.name == 'connected':
            return True

    def _received_printer_ready(self, id):
        def wait_for_id(event):
            if (
                (
                    'result' in event.data and
                    event.data['result'].get('state') == 'ready' and
                    event.data.get('id') == id
                ) or (
                    event.data.get('method') == 'notify_klippy_ready'
                )
            ):
                return True
        return wait_for_id

    def _received_websocket_id(self, event):
        if 'websocket_id' in event.data.get('result', ()):
            self.websocket_id = event.data['result']['websocket_id']
            return True

    def _received_printer_objects(self, event):
        if 'objects' in event.data.get('result', ()):
            self.printer_objects = event.data['result']['objects']
            _logger.info(f'printer objects: {self.printer_objects}')
            return True

    def _received_heaters(self, event):
        if 'heaters' in event.data.get('result', {}).get('status', {}):
            self.heaters = event.data['result']['status']['heaters']['available_heaters']  # noqa: E501
            _logger.info(f'heaters: {self.heaters}')
            return True

    def _received_subscription(self, sub_id):
        def wait_for_sub_id(event):
            if 'result' in event.data and event.data.get('id') == sub_id:
                return True
        return wait_for_sub_id

    def _received_last_job(self, event):
        if 'jobs' in event.data.get('result', {}):
            jobs = event.data.get('result', {}).get('jobs', [None]) or [None]
            self.on_event(
                Event(sender=self.id, name='last_job', data=jobs[0])
            )
            return True

    def _jsonrpc_request(self, method, **params):
        if not self.conn:
            return

        next_id = self.next_id()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next_id
        }

        if params:
            payload['params'] = params

        self.conn.send(payload)
        return next_id

    def request_websocket_id(self):
        return self._jsonrpc_request('server.websocket.id')

    def request_printer_info(self):
        return self._jsonrpc_request('printer.info')

    def request_printer_objects(self):
        return self._jsonrpc_request('printer.objects.list')

    def request_heaters(self):
        objects = {'heaters': None}
        return self._jsonrpc_request('printer.objects.query', objects=objects)

    def request_subscribe(self, objects=None):
        objects = objects if objects else {
            'print_stats': ('state', 'message', 'filename'),
            'webhooks': ('state', 'state_message'),
            'history': None,
        }
        return self._jsonrpc_request('printer.objects.list', objects=objects)

    def request_status_update(self, objects=None):
        if objects is None:
            objects = {
                "webhooks": None,
                "print_stats": None,
                "virtual_sdcard": None,
                "display_status": None,
                "heaters": None,
                "toolhead": None,
                "extruder": None,
                "gcode_move": None,
            }

            for heater in (self.heaters or ()):
                objects[heater] = None

        return self._jsonrpc_request('printer.objects.query', objects=objects)

    def request_pause(self):
        return self._jsonrpc_request('printer.print.pause')

    def request_cancel(self):
        return self._jsonrpc_request('printer.print.cancel')

    def request_resume(self):
        return self._jsonrpc_request('printer.print.resume')

    def request_job_list(self, **kwargs):
        # kwargs: start before since limit order
        return self._jsonrpc_request('server.history.list', **kwargs)

    def request_job(self, job_id):
        return self._jsonrpc_request('server.history.get_job', uid=job_id)

    def request_jog(self, axes_dict: Dict[str, Number], is_relative: bool, feedrate: int) -> Dict:
        # TODO check axes
        command = "G0 {}".format(
            " ".join([
                "{}{}".format(axis.upper(), amt)
                for axis, amt in axes_dict.items()
            ])
        )

        if feedrate:
            command += " F{}".format(feedrate * 60)

        commands = ["G91", command]
        if not is_relative:
            commands.append("G90")

        script = "\n".join(commands)
        return self._jsonrpc_request('printer.gcode.script', script=script)

    def request_home(self, axes) -> Dict:
        # TODO check axes
        script = "G28 %s" % " ".join(
            map(lambda x: "%s0" % x.upper(), axes)
        )
        return self._jsonrpc_request('printer.gcode.script', script=script)
