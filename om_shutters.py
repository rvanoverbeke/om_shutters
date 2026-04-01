#!venv/bin/python
import json
import logging
import logging.handlers
import os
import pytz
import sys
import time
from datetime import datetime, timedelta
from suntime import Sun, SunTimeException

from sdk import OpenMoticsApi

dir_path = os.path.dirname(os.path.realpath(__file__))

SUNRISE_URL = "http://api.sunrise-sunset.org/json?lat={0}&lng={1}&date={2}&formatted=0"
CFG_FILE = os.path.join(dir_path, "config.json")
HISTORY_FILE = os.path.join(dir_path, "history.json")
LOG_FILE = os.path.join(dir_path, "openmotics.log")
HTML_FILE = "/var/www/html/index.html"
SLEEP_BETWEEN_SHUTTERS = 3


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

        self._setup_logging()

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
        christmas = cfg.get("christmas", {})
        self.christmas_start = christmas.get("start")
        self.christmas_end = christmas.get("end")
        self.christmas_down = christmas.get("down")
        self.christmas_shutters = christmas.get("shutters", [])

    def _setup_logging(self):
        # Setup logging
        log_level = logging.DEBUG if self.debug is True else logging.INFO
        self.logger = logging.getLogger("om_shutters")
        self.logger.setLevel(log_level)
        fmt = logging.Formatter(
            fmt='%(asctime)s %(levelname)s %(name)s: %(message)s ( %(filename)s:%(lineno)d)',
            datefmt="%Y-%m-%d %H:%M:%S")
        handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512000, backupCount=5)
        handler.setLevel(log_level)
        handler.setFormatter(fmt)
        self.logger.addHandler(handler)
        if self.debug is True:
            stdout_handler = logging.StreamHandler(sys.stdout)
            stdout_handler.setLevel(log_level)
            stdout_handler.setFormatter(fmt)
            self.logger.addHandler(stdout_handler)

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
            self.logger.debug("Output [{}] was set on: {}".format(output, last_set))
            last_set = self._read_date(last_set)
        return last_set is None or date > last_set + timedelta(hours=15)

    def _add_history(self, output, date):
        with open(HISTORY_FILE, 'r') as content:
            history = json.load(content)
            history[str(output)] = date
            self.logger.debug("Logging Output [{}] on: {}".format(output, date))

        with open(HISTORY_FILE, 'w+') as fh:
            json.dump(history, fh)

    def _trigger_blinds(self, room, output):
        self.logger.info("Triggering blind with output [{}] in room: [{}]".format(output, room))
        local_now = datetime.now()
        if not self._check_history(output, local_now):
            self.logger.info("Blind was already triggered")
            return False
        elif self.dry_run is not False:
            self.logger.warning("DRY RUN; not doing anything")
            return False
        else:
            self.api.set_output(output, True)
            self._add_history(output, self._write_date(local_now))
        return True

    def _trigger_all_blinds(self, blinds):
        for room, output in blinds:
            if self._trigger_blinds(room, output):
                self.logger.debug("Blind was triggered, sleeping for {} seconds...\n".format(SLEEP_BETWEEN_SHUTTERS))
                time.sleep(SLEEP_BETWEEN_SHUTTERS)

    def _parse_day_month(self, local_now_dt, value):
        if not value:
            return None
        try:
            tz = pytz.timezone("Europe/Brussels")

            day, month = [int(val) for val in value.split('/', 1)]
            dt = datetime(local_now_dt.year, month, day, 0, 0)
            dt_localized = tz.localize(dt)
            dt_utc = dt_localized.astimezone(pytz.utc)
            self.logger.debug("{} was parsed as {}".format(value, dt_utc))
            return dt_utc.replace()
        except ValueError:
            self.logger.exception("Unable to parse {} as \"day/month\"".format(value))
            return None

    def _is_christmas(self, local_now_dt):
        start = self._parse_day_month(local_now_dt, self.christmas_start)
        end = self._parse_day_month(local_now_dt, self.christmas_end)
        if start and end and start <= local_now_dt and end > local_now_dt:
            return True
        return False

    def _parse_hour_minute(self, local_now_dt, value):
        if not value:
            return None
        try:
            tz = pytz.timezone("Europe/Brussels")

            hour, minute = [int(val) for val in value.split(":", 1)]
            dt = datetime(local_now_dt.year, local_now_dt.month, local_now_dt.day, hour, minute)
            dt_localized = tz.localize(dt)
            dt_utc = dt_localized.astimezone(pytz.utc)
            self.logger.debug("{} was parsed as {}".format(value, dt_utc))
            return dt_utc.replace()
        except ValueError:
            self.logger.exception("Unable to parse {} as \"hour:minute\"".format(value))
            return None

    def _find_blinds_to_rise(self, sunrise_dt, local_now_dt, blinds_to_shut):
        """ Return a list of blinds to automatically rise. Returns an empty list of nothing needs to be done."""
        blinds_to_rise = []
        rooms_shut = [room for (room, _) in blinds_to_shut]

        is_sunrise = sunrise_dt <= local_now_dt
        if not is_sunrise:
            self.logger.debug("Sun hasn't risen yet. Skipping...")
            return blinds_to_rise

        for room, (up, down, auto_up, auto_down, earliest_up, latest_down) in self.shutters.items():
            if room in rooms_shut:
                self.logger.debug("Shutter in room [{}] was just shut, not rising.".format(room))
                continue
            self.logger.debug("Checking if shutter in room [{}] needs to be raised".format(room))
            if not auto_up:
                self.logger.debug("[{}] - auto-up is disabled. Skipping...".format(room))
                continue
            earliest_up_dt = self._parse_hour_minute(local_now_dt, earliest_up)
            if earliest_up_dt is not None and earliest_up_dt > local_now_dt:
                self.logger.debug("[{}] - Should only be raised on {}. Skipping...".format(room, earliest_up_dt))
                continue
            self.logger.info("[{}] - Should be raised".format(room))
            blinds_to_rise.append((room, up))
        return blinds_to_rise

    def _find_blinds_to_shut(self, sunset_dt, local_now_dt):
        """ Return a list of blinds to automatically rise. Returns an empty list of nothing needs to be done."""
        blinds_to_shut = []

        is_sunset = sunset_dt <= local_now_dt
        if not is_sunset:
            self.logger.debug("Sun hasn't set yet. Skipping...")
            return blinds_to_shut

        for room, (up, down, auto_up, auto_down, earliest_up, latest_down) in self.shutters.items():
            self.logger.debug("Checking if shutter in room [{}] needs to be shut".format(room))
            if not auto_down:
                self.logger.debug("[{}] - auto-down is disabled. Skipping...".format(room))
                continue
            latest_down_dt = self._parse_hour_minute(local_now_dt, latest_down)
            earliest_down_dt = None
            if self._is_christmas(local_now_dt) and room in self.christmas_shutters:
                earliest_down_dt = self._parse_hour_minute(local_now_dt, self.christmas_down)
            if earliest_down_dt is not None and earliest_down_dt > local_now_dt:
                self.logger.info("[{}] - Should not be shut yet before {}, it's Christmas!".format(room, earliest_down_dt))
                continue
            if latest_down_dt is not None and latest_down_dt < local_now_dt:
                self.logger.info("[{}] - Should be shut on {}.".format(room, latest_down_dt))
                blinds_to_shut.append((room, down))
                continue
            self.logger.info("[{}] - Should be shut".format(room))
            blinds_to_shut.append((room, down))
        return blinds_to_shut

    def run(self):
        html_out = []

        local_now_dt = datetime.now(pytz.utc)

        sun = Sun(self.latitude, self.longitude)

        # Get today's sunrise and sunset in UTC
        sunrise_dt = sun.get_sunrise_time()
        sunset_dt = sun.get_sunset_time()

        # Make sure sunset/sunrise are set to current day
        current_date = datetime(local_now_dt.year, local_now_dt.month, local_now_dt.day)
        sunset_date = datetime(sunset_dt.year, sunset_dt.month, sunset_dt.day)
        sunrise_date = datetime(sunrise_dt.year, sunrise_dt.month, sunrise_dt.day)
        if current_date != sunset_date:
            offset = current_date - sunset_date
            sunset_dt += offset
        if current_date != sunrise_date:
            offset = current_date - sunrise_date
            sunrise_date += offset

        self.logger.info("Local time: {}".format(local_now_dt))
        self.logger.info("Sunrise: {}".format(sunrise_dt))
        self.logger.info("Sunset {}".format(sunset_dt))

        html_out.append("Local time: {}".format(local_now_dt))
        html_out.append("Sunrise: {}".format(sunrise_dt))
        html_out.append("Sunset {}".format(sunset_dt))

        blinds_to_shut = self._find_blinds_to_shut(sunset_dt, local_now_dt)
        if blinds_to_shut:
            self.logger.debug("Blinds to shut: {}".format(blinds_to_shut))
            self.logger.info("Shutting blinds: {}".format([blind[0] for blind in blinds_to_shut]))
            self._trigger_all_blinds(blinds_to_shut)
        else:
            self.logger.debug("Nothing to shut")


        blinds_to_rise = self._find_blinds_to_rise(sunrise_dt, local_now_dt, blinds_to_shut)
        if blinds_to_rise:
            self.logger.debug("Blinds to rise: {}".format(blinds_to_rise))
            self.logger.info("Rising blinds: {}".format([blind[0] for blind in blinds_to_rise]))
            self._trigger_all_blinds(blinds_to_rise)
        else:
            self.logger.debug("Nothing to rise")

        if not blinds_to_rise and not blinds_to_shut:
            self.logger.info("Nothing to do")
        self.logger.info("Finished")

        with open(HTML_FILE, 'w') as fh:
            for line in html_out:
                fh.write("<h2>{}</h2>\n".format(line))


if __name__ == '__main__':
    oms = OpenMoticsShutter()
    oms.run()
