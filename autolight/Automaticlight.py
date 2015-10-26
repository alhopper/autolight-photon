#!/usr/bin/env python

import datetime
import os
import sys
import re
import logging
import logging.handlers
import string
import subprocess
import urllib
import json

import ephem
import librato
import Sched

# pylint: disable=C0301
# pylint: disable=C0103


def log_inet_address(log):
    '''
        We're using DHCP addressing for the RPi board. So it's good to log the assigned
        IP address periodically.  In most cases it'll never change ... but.. in case
        it does, we'll be able to look at the logs and see its current address.
    '''
    proc = subprocess.Popen(['/sbin/ifconfig', '-a'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ifconfig_lines = proc.communicate()[0].splitlines()
    for line in ifconfig_lines:
        if (line.find('inet addr') > 0) and (line.find('127.0.0') < 0):
            icu  = re.search(r'(^\s+inet addr:)(\d+\.\d+\.\d+\.\d+)', line)
            log.info('VM ethernet address: {}'.format(icu.group(2)))


def log_uptime(log):
    '''
        Let's log the uptime 
    '''
    proc = subprocess.Popen(['/usr/bin/uptime'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log.info('RPi uptime: {}'.format(proc.communicate()[0]))


def librato_connect():
    '''
        Connect to the Librato service. See https://metrics.librato.com/
    '''
    apikey = os.getenv('LIBRATOKEY')
    libratoapi = librato.connect('al@logical-approach.com', apikey)
    return libratoapi


def get_load_level():
    '''
        Get the average load level for the last 1 minute (3rd last field)
    '''
    proc = subprocess.Popen(['/usr/bin/uptime'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    uptime = proc.communicate()[0].strip()
    return uptime.translate(None, ',').split()[-3]


def get_hostname():
    '''
        Get the VM hostname
    '''
    result = subprocess.Popen(['/bin/hostname'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    hostname = result.communicate()[0].rstrip('\n')
    return hostname

def heartbeat(libratoapi):
    '''
        We log the instantaneous load level just to get a "heart-beat" that
        changes.  This allows us to quickly eyeball the librato "activity" plot and verify
        that the VM is alive.  
    '''
    loadlevel = get_load_level()
    hostname = get_hostname()
    try:
        libratoapi.submit(hostname, float(loadlevel), description=hostname)
    except Exception as e:
        log.error('Librato: %s\n' % str(e))


def calc_sunrise_sunset(log):
    '''
        Get todays sunrise and sunset times in the local time zone
        for a given (hard-coded) latitude/longitude and (optional) elevation.
    '''
    obsvr = ephem.Observer()
    # this is the lat/long and (optional) elevation for *your* location - in this case Plano TX
    obsvr.lat, obsvr.long, obsvr.date, obsvr.elevation =  '33:2:7', '-96:44:8', ephem.now(), 600
    sun = ephem.Sun(obsvr)
    sunrise = ephem.localtime(obsvr.next_rising(sun))
    sunset = ephem.localtime(obsvr.next_setting(sun))
    log.info('Sunrise: %s, Sunset: %s\n' % (sunrise, sunset))
    return sunrise, sunset

def geturl():
    '''
        Note that the last param in the URL is the program loaded on the Photon.
        In this case the application is called 'relay'.
    '''
    return 'https://api.particle.io/v1/devices/{}/relay'.format(os.getenv('PHOTONID'))

def call_turn_on_ssr(log):
    '''
        Use the particle.io cloud API to send a command to the Photon which runs an application
        called "relay" which will use hardware pin-6 to control the Solid State Relay (SSR).
    '''
    log.info('Turn ON Photon')
    at = os.getenv('PARTICLEIOACCESS')
    params = urllib.urlencode({
        'access_token'  : at,
        'params'        : 'r4,HIGH'
    })
    jdata = json.loads(urllib.urlopen(geturl(), params).read())
    log.info('Particle IO API call returned: {}'.format(jdata['return_value']))


def call_turn_off_ssr(log):
    '''
        Turn off the Solid State Relay
    '''
    log.info('Turn Off Photon')
    at = os.getenv('PARTICLEIOACCESS')
    params = urllib.urlencode({
        'access_token'  : at,
        'params'        : 'r4,LOW'
    })
    jdata = json.loads(urllib.urlopen(geturl(), params).read())
    log.info('Particle IO API call returned: {}'.format(jdata['return_value']))


def set_current_state(log):
    '''
        OK - so the autolight controller VM came online - possibly after a power failure.
        We need to figure out if the current state of the light should be ON or OFF.
        We calculate the next 'event' - either sunrise or sunset and figure out which
        one is next.  This tells us the *next* state and, by inference, the required current state.
    '''
    now = datetime.datetime.now()
    sunrise, sunset = calc_sunrise_sunset(log)
    tsunset = sunset - now
    tsunrise = sunrise - now
    if tsunset < tsunrise:
        call_turn_off_ssr(log)
        state = 'off'
    else:
        call_turn_on_ssr(log)
        state = 'on'
    log.info('Current state is {}'.format(state))


def setup_logging():
    log = logging.getLogger('backyard-light')
    log.setLevel(logging.DEBUG)
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(name)s %(levelname)s %(message)s')
    handler.setFormatter(formatter)
    log.addHandler(handler)
    return log


def schedule_next_cycle(log):
    '''
        After the scheduler runs the next required sunrise/sunset (turn) on/off
        cycle, it needs to re-schedule upcoming event(s).  We'll run a 'cron style' job
        twice every 24 hours - just in case we lose power and come
        back online.  Note: duplicate scheduler entries are avoided.
    '''
    sunrise, sunset = calc_sunrise_sunset(log)
    sched = Sched.get()
    if not job_already_scheduled(sunrise):
        sched.add_date_job(call_turn_off_ssr, sunrise, [log])
    if not job_already_scheduled(sunset):
        sched.add_date_job(call_turn_on_ssr, sunset, [log])
    log_scheduled_jobs(log)


def job_already_scheduled(jobtime):
    '''
        Determine if a job is already scheduled
    '''
    jobs = Sched.get().get_jobs()
    found = False
    for job in jobs:
        pos = string.find(str(job), str(jobtime))
        if pos >= 0:
            found = True
            break
    return found


def log_scheduled_jobs(log):
    jobs = Sched.get().get_jobs()
    for job in jobs:
        log.info('Job %s' % str(job))


def display_scheduled_jobs(log):
    '''
        It's nice to see what's going on in the hours before the lights will be switched
        on or off.  So, display the scheduler Q on the following, cron style, schedule.
    '''
    crontimes = '7,19,20'
    Sched.get().add_cron_job(log_scheduled_jobs, hour=crontimes, minute=3, args=[log])
    log.info('Scheduled jobs will be logged per this (cron style - hourly) schedule: {}'.format(crontimes))


def check_required_environment_vars():
    required_envvars = ['LIBRATOKEY', 'PHOTONID', 'PARTICLEIOACCESS']
    for envvar in required_envvars:
        data = os.getenv(envvar)
        if data == None:
            writestr = 'Error: {} environment variable is not set!!\nProgram will terminate!\n'.format(envvar)
            sys.stderr.write(writestr)
            sys.exit(1)


def main():
    '''
        Main entry point.
    '''
    check_required_environment_vars()
    log = setup_logging()
    sched = Sched.get()  # snag a Scheduler instance
    log.info('>>')
    log.info('>>>>>>>>>>>>>>> AutoMaticLight starting <<<<<<<<<<<<<<<<')
    log.info('<<')
    log_inet_address(log)
    log_uptime(log)
    libratoapi = librato_connect()
    heartbeat(libratoapi)
    sched.add_interval_job(heartbeat, seconds=20, args=[libratoapi])
    schedule_next_cycle(log)
    sched.add_cron_job(schedule_next_cycle, hour='1,13', args=[log])
    log.info('Sunrise/Sunset calculation will run at 1:00AM and 1:00PM')
    sched.add_interval_job(log_inet_address, hours=24, args=[log])
    sched.add_interval_job(log_uptime, hours=24, args=[log])
    display_scheduled_jobs(log)
    set_current_state(log)
    return log

