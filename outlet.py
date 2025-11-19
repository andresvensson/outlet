import datetime
import os
import logging
import sys
import time
from datetime import timedelta
from logging import warning

import pymysql
import sqlite3

# debug import
import traceback

import secret as s
from phue import Bridge

# CONFIG

# hours to sleep if lamp toggled by homepage:
interruption_delay = 8
LIGHT_ID = s.unit_id()

# sleep hours when lamp not allowed to turn on by this code
SLEEP_FROM = "23:00"
SLEEP_TO = "08:00"

developing = s.settings()
# path for local database
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(BASE_DIR, "log.log")
DB_FILE = os.path.join(BASE_DIR, "local_cache.sqlite")
BRIDGE_PATH = os.path.join(BASE_DIR, 'phue.conf')
BRIDGE = Bridge(s.url(), config_file_path=BRIDGE_PATH)


def main():
    while True:
        logging.info("in main loop")
        ts_now = datetime.datetime.now()

        # default sleep time 1 hour
        sleep = 3600 # 1 hour

        interrupt_data = check_interrupts()

        # Recent toggle detected?
        if interrupt_data['active_ban']:
            logging.info("set sleep due to interrupt")
            break_time = interrupt_data['hueDB_break_time']
            if break_time > ts_now:
                sleep = (break_time - ts_now).total_seconds() + 5
                logging.warning(f"sleeping until: {break_time} due to changes made within {interruption_delay} hours")
                if developing:
                    logging.warning(
                        f"DEV MODE, skipping sleep for {round((sleep / 60 / 60))} hours (to {interrupt_data['hueDB_break_time']})")
                else:
                    time.sleep(sleep)
        else:
            # has no data from database or no toggle detected. Follow hard coded schema
            logging.info("No information from HUE database or Lamp has not been toggled for the set delay time. Proceed to check daylight")

        try:
            check_status()
        except Exception as e:
            logging.error(f"could not get status: {e}")

        if developing:
            if sleep > 600:
                msg = f"{round((sleep / 60) / 60)} hours"
            elif 599 > sleep > 60:
                msg = f"{round(sleep / 60)} min"
            else:
                msg = f"{sleep} sec"
            print(f"DEV STOP - would sleep {msg}")
            logging.warning(f"DEV STOP - would sleep {msg}")
            break

        time.sleep(sleep)


def check_interrupts() -> dict:
    logging.info(f"check if any choices was made via homepage. Interruption delay set for {interruption_delay} hours")
    h, u, p, d = s.sql_lampdb()
    sql = None

    try:
        db = pymysql.connect(host=h, user=u, passwd=p, db=d)
        c = db.cursor()
        c.execute(f"SELECT * FROM eventlog WHERE unit_id={LIGHT_ID} ORDER BY value_id DESC LIMIT 1")
        sql = c.fetchone()
        c.close()

    except pymysql.Error as e:
        msg = f"Error reading DB\n{e}"
        print(msg)
        logging.warning(msg)

    d = {'active_ban': False}

    if sql:
        d['hueDB_value_id'] = sql[0]
        d['hueDB_ts_db'] = sql[1]
        d['hueDB_ts_code'] = sql[2]
        d['hueDB_unit_id'] = sql[3]
        d['hueDB_event'] = sql[4]

        d['hueDB_break_time'] = d['hueDB_ts_code'] + datetime.timedelta(hours=interruption_delay)
        if d['hueDB_break_time'] > datetime.datetime.now():
            d['active_ban'] = True
        else:
            logging.info(f"interrupt time not relevant [{d['hueDB_break_time']}]")

        if developing:
            print("...............DATA...............")
            for x in d:
                print("KEY:", x, ":", "VAL:", d[x], "TYPE:", type(d[x]))
            print("..................................")

    else:
        logging.warning("No info from hue database. Don´t know if lamp manually toggled")

    return d


def check_status():
    d = get_daylight()

    ts_now = datetime.datetime.now()
    if d['sunrise'] < ts_now < d['sunset']:
        logging.info("its daylight")
        d['daylight'] = True
        d['nightfall'] = False
    else:
        logging.info("its nighttime")
        d['daylight'] = False
        d['nightfall'] = True

    # Convert strings → time objects
    t_from = datetime.datetime.strptime(SLEEP_FROM, "%H:%M").time()
    t_to = datetime.datetime.strptime(SLEEP_TO, "%H:%M").time()

    now = ts_now.time()

    # Check range, including crossing midnight
    if t_from < t_to:
        # Normal range (e.g., 07:00 → 22:00)
        sleeping = t_from <= now < t_to
    else:
        # Cross-midnight range (23:00 → 08:00)
        sleeping = now >= t_from or now < t_to

    d['ban_time'] = sleeping


    if developing:
        print("...............DATA...............")
        for x in d:
            print("KEY:", x, ":", "VAL:", d[x], "TYPE:", type(d[x]))
        print("..................................")

    set_state(d)


def get_daylight() -> dict:
    logging.info("Get sunrise and sunset times")
    init_cache()
    # check sqlite if data old, get daylight from db, store it in sql
    d = load_cache()

    ts_now = datetime.datetime.now()
    if d:
        logging.info("Got cached values")
        if d['timestamp'] > ts_now + timedelta(days=-1):
            logging.info("Using cached values")
            return d
        else:
            logging.warning("Cached data is old")
    else:
        logging.info("Connect to remote database instead")
    try:
        d = get_remote_data()
        return d

    except Exception as e:
        print(f"Error get remote data\n{e}")
        logging.exception(f"Error get remote data\n{e}")
        if d:
            logging.warning("⚠ Using old cached values due to remote error")
            return d

    logging.exception("⚠ Using hard coded values due to remote error and no cached data")
    d['sunrise'] = ts_now.replace(hour=7, minute=30, second=0)
    d['sunset'] = ts_now.replace(hour=18, minute=0, second=0)

    return d


def get_remote_data() -> dict:
    logging.info("Connect to remote db and set day or night status")
    h, u, p, d = s.sql()
    sql = None

    try:
        # TODO
        db = pymysql.connect(host=h, user=u, passwd=p, db=d)
        c = db.cursor()
        c.execute("SELECT value_id, time_stamp, sunrise, sunset "
                  "FROM weather_outside ORDER BY value_id DESC LIMIT 1")
        sql = c.fetchone()
        c.close()

    except pymysql.Error as e:
        logging.warning(f"Error reading DB: {e}\nFallback to default schema")

    d = {}
    if sql:
        d['timestamp'] = sql[1]
        # compensate for sunset/sunrise being timedelta object
        # also add 1 hour for timezone corrections
        sr = sql[2] + datetime.timedelta(hours=1)
        ss = sql[3] + datetime.timedelta(hours=1)

        d['sunrise'] = datetime.datetime.combine(datetime.date.today(), (datetime.datetime.min + sr).time())
        d['sunset'] = datetime.datetime.combine(datetime.date.today(), (datetime.datetime.min + ss).time())

        save_cache(d['timestamp'], d['sunrise'], d['sunset'])

    return d


def init_cache():
    logging.info(f"initiate sqllite3 database ({DB_FILE})")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_cache (
            cache_date TEXT PRIMARY KEY,
            timestamp TEXT,
            sunrise TEXT,
            sunset TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_cache(timestamp: datetime, sunrise: datetime, sunset: datetime):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    today = datetime.date.today().isoformat()

    cur.execute("""
        INSERT OR REPLACE INTO daily_cache (cache_date, timestamp, sunrise, sunset)
        VALUES (?, ?, ?, ?)
    """, (today,
          timestamp.isoformat(),
          sunrise.isoformat(),
          sunset.isoformat()))

    conn.commit()
    conn.close()
    logging.info("Values cached")


def load_cache():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    today = datetime.date.today().isoformat()

    cur.execute("SELECT timestamp, sunrise, sunset FROM daily_cache WHERE cache_date = ?", (today,))
    row = cur.fetchone()
    conn.close()

    if not row:
        logging.warning("No cached values found")
        return None

    ts, sunrise, sunset = row
    return {
        "timestamp": datetime.datetime.fromisoformat(ts),
        "sunrise": datetime.datetime.fromisoformat(sunrise),
        "sunset": datetime.datetime.fromisoformat(sunset)
    }


def set_state(data):
    logging.info("Connect to lamp")
    hue = None
    # If the app is not registered and the button is not pressed,
    # press the button and call connect()
    # (this only needs to be run a single time)
    try:
        BRIDGE.connect()
        hue = BRIDGE.get_light(LIGHT_ID, 'on')

    except Exception as e:
        traceback.format_exc()
        logging.error(f"Error, Could not reach hue lamp: {e}")

    if hue:
        logging.info("Lamp state: ON")
        if data['daylight']:
            logging.info("Its daylight so lamp should be turned off")
            turn_off()
        else:
            logging.info("and its nightfall so all good")
    else:
        logging.info("lamp state: OFF")
        if data['nightfall'] and not data['ban_time']:
            logging.info("Its nightfall and outside ban time so lamp should be turned on")
            turn_on()
        else:
            logging.info("and its daylight or ban time so lamp toggle should not be done")
            pass

def turn_on():
    logging.info("Send ON signal to lamp")
    BRIDGE.set_light(LIGHT_ID, 'on', True)
    print("Turn on lamp")

def turn_off():
    logging.info("Send OFF signal to lamp")
    BRIDGE.set_light(LIGHT_ID, 'on', False)
    print("Turn off lamp")


if __name__ == "__main__":
    if developing:
        logging.basicConfig(level=logging.DEBUG, filename=log_path, filemode="w",
                            format="%(asctime)s - %(levelname)s - %(message)s")
    else:
        #TODO: change INFO -> WARNING
        logging.basicConfig(level=logging.WARNING, filename=log_path, filemode="w",
                            format="%(asctime)s - %(levelname)s - %(message)s")
    logging.info("lamp.py stared")
    main()
