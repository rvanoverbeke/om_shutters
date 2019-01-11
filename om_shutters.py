#!/usr/bin/python
import json
import pytz
import requests
import syslog
import time
from datetime import datetime, timedelta

from sdk import OpenMoticsApi, OpenMoticsCloudApi, traceback

SUNRISE_URL = 'http://api.sunrise-sunset.org/json?lat={0}&lng={1}&date={2}&formatted=0'
CFG_FILE = "/home/rick/om_shutters/config.json"
HISTORY_FILE = '/home/rick/om_shutters/history.json'


class OpenMoticsShutter(object):
    def __init__(self):
        # These can and should be overridden in config.json
        self.dry_run = True
        self.debug = True

        self.username = None
        self.password = None
        self.om_host = None

        self.latitude = None
        self.longitude = None

        self.shutters = {}

        self._load_cfg()

        self.api = OpenMoticsApi(self.username, self.password, self.om_host, False)

    def _load_cfg(self):
        with open(CFG_FILE, 'r') as fh:
            cfg = json.load(fh)

        self.dry_run = cfg.get("dry_run", True)
        self.debug = cfg.get("debug", True)

        credentials = cfg.get("credentials", {})
        self.username = credentials.get("username")
        self.password = credentials.get("password")
        self.om_host = credentials.get("om_host")

        location = cfg.get("location", {})
        self.latitude = int(location.get("latitude", "0"))
        self.longitude = int(location.get("longitude", "0"))

        self.shutters = cfg.get("shutters", {})


    def _log(self, msg):
        if self.debug is True:
            print "[{}] {}".format(datetime.now(), msg)
        else:
            syslog.syslog(msg)

    @staticmethod
    def _read_date(date):
        if date is None:
            return None
        return datetime.strptime(date, '%Y-%m-%dT%H:%M:%S+00:00')

    @staticmethod
    def _write_date(date):
        return date.strftime('%Y-%m-%dT%H:%M:%S+00:00')

    def _check_history(self, output, date):
        with open(HISTORY_FILE, 'r') as content:
            history = json.load(content)
            last_set = history.get(str(output), None)
            self._log("Output [{}] was set on: {}".format(output, last_set))
            last_set = self._read_date(last_set)
        return last_set is None or date > last_set + timedelta(hours=20)

    def _add_history(self, output, date):
        with open(HISTORY_FILE, 'r') as content:
            history = json.load(content)
            history[str(output)] = date
            self._log("Logging Output [{}] on: {}".format(output, date))

        with open(HISTORY_FILE, 'w+') as fh:
            json.dump(history, fh)

    def _trigger_blinds(self, room, output):
        self._log("Triggering blind with output [{}] in room: [{}]".format(output, room))
        local_now = datetime.now()
        if not self._check_history(output, local_now):
            self._log("Blind was already triggered")
            return False
        elif self.dry_run is not False:
            self._log("DRY RUN; not doing anything")
            return False
        else:
            api.set_output(output, True)
            self._add_history(output, _write_date(local_now))
        return True

    def _shut_all_blinds(self):
        self._log("Shutting down blinds\n")
        for room in self.shutters:
            up, down = self.shutters[room]
            self._trigger_blinds(room, down)
            self._log("Sleeping for 3 seconds...\n")
            time.sleep(3)

    def _rise_all_blinds(self):
        self._log("Rising up blinds\n")
        for room in self.shutters:
            up, down = self.shutters[room]
            self._trigger_blinds(room, up)
            self._log("Sleeping for 3 seconds...\n")
            time.sleep(3)

    def run(self):
        local_now_dt = datetime.now()
        url = SUNRISE_URL.format(self.latitude, self.longitude, local_now_dt.strftime('%Y-%m-%d'))
        data = requests.get(url).json()
        sunrise = data['results']['sunrise']
        sunset = data['results']['sunset']
        sunrise_dt = self._read_date(sunrise)
        sunset_dt = self._read_date(sunset)

        self._log("Local time: {}".format(local_now_dt))
        self._log("Sunrise: {}".format(sunrise_dt))
        self._log("Sunset {}".format(sunset_dt))

        if sunset_dt <= local_now_dt:
            self._log("Sun is setting")
            self._shut_all_blinds()
        elif sunrise_dt <= local_now_dt:
            self._log("Sun is rising")
            self._rise_all_blinds()
        else:
            self._log("Nothing to do")
            self._log("Finished")


if __name__ == '__main__':
    oms = OpenMoticsShutter()
    oms.run()
