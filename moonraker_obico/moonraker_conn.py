from typing import Optional, Dict, List, Tuple
from numbers import Number
import dataclasses
import re
import queue
import threading
import requests  # type: ignore
import logging
import time
import backoff
import json
import bson
import websocket
from random import randrange
from collections import deque, OrderedDict
from functools import reduce
from operator import concat
import subprocess
import os

from .utils import DEBUG, run_in_thread
from .ws import WebSocketClient, WebSocketConnectionException


REQUEST_STATE_INTERVAL_SECONDS = 30
if DEBUG:
    REQUEST_STATE_INTERVAL_SECONDS = 10

_logger = logging.getLogger('obico.moonraker_conn')
_ignore_pattern=re.compile(r'"method": "notify_proc_stat_update"')

class MoonrakerConn:
    flow_step_timeout_msecs = 2000
    ready_timeout_msecs = 60000

    def __init__(self, app_config, sentry, on_event):
        self.id: str = 'moonrakerconn'
        self.app_config: Config = app_config
        self.config: MoonrakerConfig = app_config.moonraker
        self.klippy_ready = threading.Event()  # Based on https://moonraker.readthedocs.io/en/latest/web_api/#websocket-setup

        self.sentry = sentry
        self._on_event = on_event
        self.shutdown: bool = False
        self.conn = None
        self.ws_message_queue_to_moonraker = queue.Queue(maxsize=16)
        self.moonraker_state_requested_ts = 0
        self.request_callbacks = OrderedDict()
        self.request_callbacks_lock = threading.RLock()   # Because OrderedDict is not thread-safe
        self.subscribed_objects = []

    ## REST API part

    def api_get(self, mr_method, timeout=5, raise_for_status=True, **params):
        url = f'{self.config.http_address()}/{mr_method.replace(".", "/")}'
        _logger.debug(f'GET {url}')

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

    def api_post(self, mr_method, timeout=None, multipart_filename=None, multipart_fileobj=None, **post_params):
        url = f'{self.config.http_address()}/{mr_method.replace(".", "/")}'
        _logger.debug(f'POST {url}')

        headers = {'X-Api-Key': self.config.api_key} if self.config.api_key else {}
        files={'file': (multipart_filename, multipart_fileobj, 'application/octet-stream')} if multipart_filename and multipart_fileobj else None
        resp = requests.post(
            url,
            headers=headers,
            data=post_params,
            files=files,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def ensure_api_key(self):
        if not self.config.api_key:
            _logger.warning('api key is unset, trying to fetch one')
            self.config.api_key = self.api_get('access/api_key', raise_for_status=True)

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def get_server_info(self):
        return self.api_get('server/info')

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    @backoff.on_predicate(backoff.expo, max_value=60)
    def wait_for_klippy_ready(self):
        return self.get_server_info().get("klippy_state") == 'ready'

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def find_all_heaters(self):
        data = self.api_get('printer/objects/query', raise_for_status=True, heaters='') # heaters='' -> 'query?heaters=' by the behavior in requests
        if 'heaters' in data.get('status', {}):
            return data['status']['heaters']
        else:
            return []

    def find_all_thermal_presets(self):
        presets = []
        data = self.api_get('server/database/item', raise_for_status=False, namespace='mainsail', key='presets') or {}
        for preset in data.get('value', {}).get('presets', {}).values():
            try:
                preset_name = preset['name']
                extruder_target = float(preset['values']['extruder']['value'])
                bed_target = float(preset['values']['heater_bed']['value'])
                presets.append(dict(name=preset_name, heater_bed=bed_target, extruder=extruder_target))
            except Exception as e:
                self.sentry.captureException()

        return presets

    def find_all_installed_plugins(self):
        data = self.api_get('machine/update/status', raise_for_status=False, refresh='false')
        if not data:
            return []
        return list(set(data.get("version_info", {}).keys()) - set(['system', 'moonraker', 'klipper']))

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def find_most_recent_job(self):
        data = self.api_get('server/history/list', raise_for_status=True, order='desc', limit=1)
        return (data.get('jobs', [None]) or [None])[0]

    def update_webcam_config_from_moonraker(self):
        def webcam_config_in_moonraker():
            return []

            # TODO: Rotation is not handled correctly

            # Check for the webcam API in the newer Moonraker versions
            result = self.api_get('server.webcams.list', raise_for_status=False)
            if result and len(result.get('webcams', [])) > 0:  # Apparently some Moonraker versions support this endpoint but mistakenly returns an empty list even when webcams are present
                _logger.debug(f'Found config in Moonraker webcams API: {result}')
                webcam_configs = [ dict(
                            target_fps = cfg.get('target_fps', 25),
                            snapshot_url = cfg.get('snapshot_url', None),
                            stream_url = cfg.get('stream_url', None),
                            flip_h = cfg.get('flip_horizontal', False),
                            flip_v = cfg.get('flip_vertical', False),
                            rotation = cfg.get('rotation', 0),
                         ) for cfg in result.get('webcams', []) if 'mjpeg' in cfg.get('service', '').lower() ]

                if len(webcam_configs) > 0:
                    return  webcam_configs

                # In case of WebRTC webcam
                webcam_configs = [ dict(
                            target_fps = cfg.get('target_fps', 25),
                            snapshot_url = cfg.get('snapshot_url', None),
                            stream_url = cfg.get('snapshot_url', '').replace('action=snapshot', 'action=stream'), # TODO: Webrtc stream_url is not compatible with MJPEG stream url. Let's guess it. it is a little hacky.
                            flip_h = cfg.get('flip_horizontal', False),
                            flip_v = cfg.get('flip_vertical', False),
                            rotation = cfg.get('rotation', 0),
                         ) for cfg in result.get('webcams', []) if 'webrtc' in cfg.get('service', '').lower() ]
                return  webcam_configs

            # Check for the standard namespace for webcams
            result = self.api_get('server.database.item', raise_for_status=False, namespace='webcams')
            if result:
                _logger.debug(f'Found config in Moonraker webcams namespace: {result}')
                return [ dict(
                            target_fps = cfg.get('targetFps', 25),
                            snapshot_url = cfg.get('urlSnapshot', None),
                            stream_url = cfg.get('urlStream', None),
                            flip_h = cfg.get('flipX', False),
                            flip_v = cfg.get('flipY', False),
                            rotation = cfg.get('rotation', 0), # TODO Verify the key name for rotation
                        ) for cfg in result.get('value', {}).values() if 'mjpeg' in cfg.get('service', '').lower() ]

            # webcam configs not found in the standard location. Try fluidd's flavor
            result = self.api_get('server.database.item', raise_for_status=False, namespace='fluidd', key='cameras')
            if result:
                _logger.debug(f'Found config in Moonraker fluidd/cameras namespace: {result}')
                return [ dict(
                            target_fps = cfg.get('target_fps', 25),  # TODO Verify the key name in fluidd for FPS
                            stream_url = cfg.get('url', None),
                            flip_h = cfg.get('flipX', False),
                            flip_v = cfg.get('flipY', False),
                            rotation = cfg.get('rotation', 0), # TODO Verify the key name for rotation
                        ) for cfg in result.get('value', {}).get('cameras', []) if cfg.get('enabled', False) ]

            #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable
            return []

        mr_webcam_config = webcam_config_in_moonraker()
        if len(mr_webcam_config) > 0:
            _logger.debug(f'Retrieved webcam config from Moonraker: {mr_webcam_config[0]}')
            self.app_config.webcam.moonraker_webcam_config = mr_webcam_config[0]

            # Add all webcam urls to the blacklist so that they won't be tunnelled
            url_list = [[ cfg.get('snapshot_url', None), cfg.get('stream_url', None) ] for cfg in mr_webcam_config ]
            self.app_config.tunnel.url_blacklist = [ url for url in reduce(concat, url_list) if url ]
        else:
            #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable
            pass

    def set_macro_variable(self, macro_name, var_name, var_value):
        script = f'SET_GCODE_VARIABLE MACRO={macro_name} VARIABLE={var_name} VALUE={var_value}'
        try:
            resp = self.api_post(
                'printer/gcode/script',
                raise_for_status=True,
                script=script
            )
        except:
            _logger.warning(f'set_macro_variable failed! - SET_GCODE_VARIABLE MACRO={macro_name} VARIABLE={var_name} VALUE={var_value}')

    def initialize_layer_change_macro(self, **kwargs):
        macro_is_configured = any('gcode_macro _obico_layer_change' in item.lower() for item in self.subscribed_objects)
        if not macro_is_configured:
            return

        for key, value in kwargs.items():
            self.set_macro_variable('_OBICO_LAYER_CHANGE', key, value)

    ## WebSocket part

    def start(self) -> None:

        thread = threading.Thread(target=self.message_to_moonraker_loop)
        thread.daemon = True
        thread.start()

        while self.shutdown is False:
            try:
                if self.klippy_ready.wait() and self.moonraker_state_requested_ts < time.time() - REQUEST_STATE_INTERVAL_SECONDS:
                    self.request_status_update()

            except Exception as e:
                self.sentry.captureException()

            time.sleep(1)

    def message_to_moonraker_loop(self):

        def on_mr_ws_open(ws):
            _logger.info('connection is open')

            self.wait_for_klippy_ready()

            self.app_config.update_heater_mapping(self.find_all_heaters())  # We need to find all heaters as their names have to be specified in the objects query request
            self.klippy_ready.set()

            self.request_subscribe()

        def on_mr_ws_close(ws, **kwargs):
            self.klippy_ready.clear()
            self.push_event(
                Event(sender=self.id, name='mr_disconnected', data={})
            )
            self.request_status_update()  # Trigger a re-connection to Moonraker

        def on_message(ws, raw):
            if ( _ignore_pattern.search(raw) is not None ):
                return

            data = json.loads(raw)
            _logger.debug(f'Received from Moonraker: {data}')

            callback = None

            with self.request_callbacks_lock:
                resp_id = data.get('id', -1)
                if resp_id in self.request_callbacks:
                    callback = self.request_callbacks[resp_id]
                    del self.request_callbacks[resp_id]

            if callback:
                callback(data)
                return

            self.push_event(
                Event(sender=self.id, name='message', data=data)
            )

        self.request_status_update()  # "Seed" a request in ws_message_queue_to_moonraker to trigger the initial connection to Moonraker

        while self.shutdown is False:
            try:
                data = self.ws_message_queue_to_moonraker.get()

                if not self.conn or not self.conn.connected():
                    self.ensure_api_key()

                    if not self.conn or not self.conn.connected():
                        header=['X-Api-Key: {}'.format(self.config.api_key), ]
                        self.conn = WebSocketClient(
                                    url=self.config.ws_url(),
                                    header=header,
                                    on_ws_msg=on_message,
                                    on_ws_open=on_mr_ws_open,
                                    on_ws_close=on_mr_ws_close,)

                        self.klippy_ready.wait()
                        _logger.info('Klippy ready')

                _logger.debug("Sending to Moonraker: \n{}".format(data))
                self.conn.send(json.dumps(data, default=str))
            except WebSocketConnectionException as e:
                _logger.warning(e)
            except Exception as e:
                _logger.warning(e)
                self.sentry.captureException()

    def push_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    def close(self):
        self.shutdown = True
        if not self.conn:
            self.conn.close()

    def jsonrpc_request(self, method, params=None, callback=None):
        next_id = randrange(100000)
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next_id
        }

        if params:
            payload['params'] = params

        if callback:
            with self.request_callbacks_lock:
                if len(self.request_callbacks) > 100:
                    self.request_callbacks.popitem(last=False)
                self.request_callbacks[next_id] = callback

        try:
            self.ws_message_queue_to_moonraker.put_nowait(payload)
        except queue.Full:
            _logger.warning("Moonraker message queue is full, msg dropped")


    def request_subscribe(self):
        subscribe_objects = {
            'print_stats': ('state', 'message', 'filename', 'info'),
            'webhooks': ('state', 'state_message'),
            'gcode_move': ('speed_factor', 'extrude_factor'),
            'history': None,
            'gcode_macro _OBICO_LAYER_CHANGE': None,
            'fan': ('speed'),
        }
        available_printer_objects = self.api_get('printer.objects.list', raise_for_status=False).get('objects', [])
        self.subscribed_objects = {
            key: value for key, value in subscribe_objects.items() if key in available_printer_objects
        }

        _logger.debug(f'Subscribing to objects {self.subscribed_objects}')
        self.jsonrpc_request('printer.objects.subscribe', params=dict(objects=self.subscribed_objects))

        if not 'gcode_macro _OBICO_LAYER_CHANGE' in self.subscribed_objects:
            run_in_thread(self._setup_include_cfgs)

    def request_status_update(self, objects=None):
        def status_update_callback(data):
            self.push_event(
                Event(sender=self.id, name='status_update', data=data)
            )

        self.moonraker_state_requested_ts = time.time()

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
                'gcode_macro _OBICO_LAYER_CHANGE': None,
                "fan": None,
            }

            for heater in (self.app_config.all_mr_heaters()):
                objects[heater] = None

        self.jsonrpc_request('printer.objects.query', params=dict(objects=objects), callback=status_update_callback)

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
        return self.jsonrpc_request('printer.gcode.script', params=dict(script=script))

    def request_home(self, axes) -> Dict:
        # TODO check axes
        script = "G28 %s" % " ".join(
            map(lambda x: "%s0" % x.upper(), axes)
        )
        return self.jsonrpc_request('printer.gcode.script', params=dict(script=script))

    def request_set_temperature(self, heater, target_temp) -> Dict:
        script = f'SET_HEATER_TEMPERATURE HEATER={heater} TARGET={target_temp}'
        return self.jsonrpc_request('printer.gcode.script', params=dict(script=script))

    def _setup_include_cfgs(self):
        data = self.api_get('printer.info', raise_for_status=False)
        if not data:
            _logger.warning('Aborted ensuring include_cfgs because moonraker printer/info call failed')
            return

        printer_cfg = data.get('config_file')
        if not printer_cfg:
            _logger.warning('Aborted ensuring include_cfgs because moonraker printer/info call failed')
            return

        ensure_include_cfgs_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts', 'ensure_include_cfgs.sh')
        FNULL = open(os.devnull, 'w')
        cmd = f'{ensure_include_cfgs_sh} {printer_cfg}'
        _logger.debug('Popen: {}'.format(cmd))
        proc = subprocess.Popen(cmd.split(' '), stdout=FNULL, stderr=FNULL)
        proc_exit_code = proc.wait()
        if proc_exit_code != 0:
            _logger.warning(f'{cmd} exited with {proc_exit_code}')

@dataclasses.dataclass
class Event:
    name: str
    data: Dict
    sender: Optional[str] = None
