import datetime
import os
import logging
import time
import pymysql

import secret as s
from phue import Bridge

# CONFIG
# hours to sleep if lamp toggled by homepage:
interruption_delay = 8
developing = s.settings()
# path for local database
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, "database.db")
log_path = os.path.join(BASE_DIR, "log.log")


class CtrlOutlet:
    """
    Turn on at night and off at day (+2h day?, maybe later)
    Get daylight from remote db (or fall back to a default)
    while loop, except when developing
    """
    def __init__(self) -> None:
        self.data = None
        self.interruption_data = None
        self.url = s.url()
        self.interruption_delay = interruption_delay
        self.sleep = 2.0

        while True:
            self.check_interrupts()
            self.get_daylight()
            self.set_state()
            self.sleep = self.get_sleep()

            if developing:
                # add interrupts/break data
                self.print_data()
                break
            else:
                msg = "sleep for {0} minutes".format(round(self.sleep / 60))
                logging.info(msg)
                time.sleep(self.sleep)
                print("Sleep")

        logging.info("DEV: Code completed")
        return

    def check_interrupts(self):
        logging.info("check if any choices was made via homepage. Interruption delay set for " +
                     str(self.interruption_delay) + " hours")

        h, u, p, d = s.sql_lampdb()
        db = pymysql.connect(host=h, user=u, passwd=p, db=d)
        c = db.cursor()
        sql = None

        try:
            c.execute("SELECT * FROM eventlog WHERE unit_id=1 ORDER BY value_id DESC LIMIT 1")
            sql = c.fetchone()
            c.close()

        except pymysql.Error as e:
            msg = "Error reading DB: {0}\nFallback to default schema".format(e)
            print(msg)
            logging.warning(msg)

        ts_now = datetime.datetime.now()
        d = {}
        if sql:
            d['hueDB_value_id'] = sql[0]
            d['hueDB_ts_db'] = sql[1]
            d['hueDB_ts_code'] = sql[2]
            d['hueDB_unit_id'] = sql[3]
            d['hueDB_event'] = sql[4]

            d['hueDB_break_time'] = d['hueDB_ts_code'] + datetime.timedelta(hours=self.interruption_delay)

            self.interruption_data = d

            if developing:
                sleep_time = d['hueDB_break_time'] - ts_now
                sleep_time_sec = sleep_time.total_seconds()
                return

            # Has outlet been given order by webb page within the delay period?
            else:
                if d['hueDB_break_time'] > ts_now:
                    sleep_time = d['hueDB_break_time'] - ts_now
                    sleep_time_sec = sleep_time.total_seconds()
                    logging.info("lamp should not be controlled now. Waiting")
                    time.sleep(sleep_time_sec + 5)
                else:
                    logging.info("Outlet has not been toggled for the set time. Proceed to check daylight")
                    return
        else:
            logging.warning("No info from hue database. Don´t know if lamp manually toggled")
            pass

    def set_state(self):
        logging.info("Connect to outlet")
        hue = None
        # If the app is not registered and the button is not pressed,
        # press the button and call connect()
        # (this only needs to be run a single time)
        try:
            b = Bridge(s.url())
            b.connect()
            hue = b.get_api()
        except Exception as e:
            msg = "Error, Could not reach hue Outlet: {}".format(e)
            logging.error(msg)

        if hue:
            if hue['lights']['1']['state']['on']:
                logging.info("Outlet state: ON")
                if self.data['daylight']:
                    logging.info("Its daylight so outlet should be turned off")
                    self.turn_off()
                else:
                    logging.info("and its nightfall so all good")

            if not hue['lights']['1']['state']['on']:
                logging.info("Outlet state: OFF")
                if self.data['nightfall']:
                    logging.info("Its nightfall so outlet should be turned on")
                    self.turn_on()
                else:
                    logging.info("and its daylight so all good")
                    pass
        else:
            logging.warning("No info from hue bridge")
            pass

    def turn_on(self):
        logging.info("Send ON signal to Outlet")
        Bridge(self.url).set_light(1, 'on', True)
        print("Turn on Outlet")

    def turn_off(self):
        logging.info("Send OFF signal to Outlet")
        Bridge(self.url).set_light(1, 'on', False)
        print("Turn off Outlet")

    def get_daylight(self):
        logging.info("Connect to remote db and set day or night status")

        h, u, p, d = s.sql()
        db = pymysql.connect(host=h, user=u, passwd=p, db=d)
        c = db.cursor()
        sql = None

        try:
            # TODO
            c.execute("SELECT value_id, time_stamp, sunrise, sunset, api_time "
                      "FROM weather_outside ORDER BY value_id DESC LIMIT 1")
            sql = c.fetchone()
            c.close()

        except pymysql.Error as e:
            msg = "Error reading DB: {0}\nFallback to default schema".format(e)
            print(msg)
            logging.warning(msg)

        ts_now = datetime.datetime.now()
        d = {}
        if sql:
            d['ts'] = datetime.datetime.now()
            d['raw_data'] = sql
            d['time_stamp'] = sql[1]

            d['td_sunrise'] = sql[2]
            d['td_sunset'] = sql[3]

            d['api_time'] = sql[4]

            # compensate for sunset/sunrise being timedelta object
            # also add 1 hour for timezone corrections
            ts = ts_now.replace(hour=1, minute=0, second=0, microsecond=0)
            d['sunrise'] = ts + d['td_sunrise']
            d['sunset'] = ts + d['td_sunset']

        else:
            # a default on/off time
            d['sunrise'] = ts_now.replace(hour=7, minute=30, second=0)
            d['sunset'] = ts_now.replace(hour=18, minute=0, second=0)

        if d['sunrise'] < ts_now < d['sunset']:
            logging.info("its daylight")
            d['daylight'] = True
            d['nightfall'] = False
        else:
            logging.info("its nighttime")
            d['daylight'] = False
            d['nightfall'] = True

        self.data = d

    def get_sleep(self):
        sec = 0
        ts_now = datetime.datetime.now()

        # calculate time to sunrise (same day) 00:00 -> 07:30
        if ts_now < self.data['sunrise']:
            sec = self.data['sunrise'] - ts_now

        # to sunset same day 07:30 -> 18:00
        if self.data['sunrise'] < ts_now < self.data['sunset']:
            sec = self.data['sunset'] - ts_now

        # to next the day 18:00 -> next day
        elif ts_now > self.data['sunset']:
            time_delta = datetime.timedelta(days=1)
            stop_date = ts_now + time_delta
            stop_date = stop_date.replace(hour=2, minute=5)
            sec = stop_date - ts_now

        self.sleep = sec.total_seconds() + 5
        return self.sleep

    def print_data(self):

        if isinstance(self.data, dict):
            if isinstance(self.interruption_data, dict):
                self.data.update(self.interruption_data)

            for d in self.data:
                print(d, ":", self.data[d], type(self.data[d]))
        else:
            print("data to print")
            print(self.data)


if __name__ == "__main__":
    if developing:
        logging.basicConfig(level=logging.DEBUG, filename=log_path, filemode="w",
                            format="%(asctime)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, filename=log_path, filemode="w",
                            format="%(asctime)s - %(levelname)s - %(message)s")
    logging.info("outlet.py stared standalone")
    CtrlOutlet()
