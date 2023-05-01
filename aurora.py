# -*- coding: utf-8 -*-
"""
Created on Sun Apr 30 10:11:31 2023

@author: jmine
"""

import re
import requests
import json
import datetime as dt
import pickle
from twilio.rest import Client
from geopy.geocoders import Nominatim

from config import (geocode_cache,
                    phone_locs,
                    twilio_sid, twilio_token)


def geocode_address(address, cache: dict):
    # Check if the address is a tuple of latitude and longitude
    if isinstance(address, tuple) and len(address) == 2:
        return address

    # Check if the address is a string with latitude and longitude
    match = re.match(r'(-?\d+(\.\d+)?),\s*(-?\d+(\.\d+)?)', address)
    if match:
        return float(match.group(1)), float(match.group(3))

    # Check if the address is in the cache
    if address in cache:
        return cache[address]

    # Geocode the address and cache the result
    geolocator = Nominatim(user_agent='aurora')
    location = geolocator.geocode(address)
    if location is not None:
        lat, lon = location.latitude, location.longitude
        cache[address] = (lat, lon)
        with open(geocode_cache, "wb") as f:
            pickle.dump(cache, f)
        return lat, lon

    # If the address cannot be geocoded, return None
    return None


def load_ovation():
    url = 'https://services.swpc.noaa.gov/json/ovation_aurora_latest.json'
    response = requests.get(url)
    data = json.loads(response.text)
    return data


def send_sms(phone, message):
    pass


class Ovation():
    url = 'https://services.swpc.noaa.gov/json/ovation_aurora_latest.json'

    def __init__(self):

        def format_time(iso):
            return dt.datetime.fromisoformat(iso)

        response = requests.get(Ovation.url)
        data = json.loads(response.text)

        self.observation_time = format_time(data['Observation Time'])
        self.forcast_time = format_time(data['Forecast Time'])

        return None


if __name__ == '__main__':

    # Load geocoded results from file or create empty dictionary
    try:
        with open(geocode_cache, "rb") as f:
            geocoded = pickle.load(f)
    except FileNotFoundError:
        geocoded = {}

    # Create twilio client for sending text messages
    twilio_client = Client(twilio_sid, twilio_token)

    while True:

        ovation = load_ovation()

        for phone, locations in phone_locs.items():
            lat, lon = geocode_address(location, geocoded)

        time.sleep(3600)  # wait an hour before checking again
