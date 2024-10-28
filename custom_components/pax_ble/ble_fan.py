#!/usr/bin/env python3.6

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from homeassistant.components import bluetooth
from struct import pack, unpack
import datetime
from bleak import BleakClient
from bleak.exc import BleakError
import binascii
import math
from collections import namedtuple
import logging

from .const import DeviceModel
from .devices.characteristics import *
from .devices.calima import Calima
from .devices.svara import Svara

_LOGGER = logging.getLogger(__name__)

Fanspeeds = namedtuple("Fanspeeds", "Humidity Light Trickle")
Fanspeeds.__new__.__defaults__ = (2250, 1625, 1000)
Time = namedtuple("Time", "DayOfWeek Hour Minute Second")
Sensitivity = namedtuple("Sensitivity", "HumidityOn Humidity LightOn Light")
LightSensorSettings = namedtuple("LightSensorSettings", "DelayedStart RunningTime")
HeatDistributorSettings = namedtuple(
    "HeatDistributorSettings", "TemperatureLimit FanSpeedBelow FanSpeedAbove"
)
SilentHours = namedtuple(
    "SilentHours", "On StartingHour StartingMinute EndingHour EndingMinute"
)
TrickleDays = namedtuple("TrickleDays", "Weekdays Weekends")
BoostMode = namedtuple("BoostMode", "OnOff Speed Seconds")

FanState = namedtuple("FanState", "Humidity Temp Light RPM Mode")

class BleFan:
    device = {}

    def __init__(self, hass, model:str, mac, pin):
        self._hass = hass
        self._dev = None

        self._model = model
        self._mac = mac
        self._pin = pin

        match model:
            case DeviceModel.CALIMA.value:
                self.device = Calima()
            case DeviceModel.SVARA.value:
                self.device = Svara()
            case _:
                raise ValueError(f"Unsupported device model: {model}")

    async def authorize(self):
        await self.setAuth(self._pin)

    async def connect(self, retries=3) -> bool:
        if self.isConnected():
            return True

        tries = 0

        _LOGGER.debug("Connecting to %s", self._mac)
        while tries < retries:
            tries += 1

            try:
                d = bluetooth.async_ble_device_from_address(
                    self._hass, self._mac.upper()
                )
                if not d:
                    raise BleakError(
                        f"A device with address {self._mac} could not be found."
                    )
                self._dev = BleakClient(d)
                ret = await self._dev.connect()
                if ret:
                    _LOGGER.debug("Connected to %s", self._mac)
                    break
            except Exception as e:
                if tries == retries:
                    _LOGGER.info("Not able to connect to %s! %s", self._mac, str(e))
                else:
                    _LOGGER.debug("Retrying %s", self._mac)

        return self.isConnected()

    async def disconnect(self) -> None:
        if self._dev is not None:
            try:
                await self._dev.disconnect()
            except Exception as e:
                _LOGGER.info("Error disconnecting from %s! %s", self._mac, str(e))
            self._dev = None

    def isConnected(self) -> bool:
        if self._dev is not None and self._dev.is_connected:
            return True
        else:
            return False

    def _bToStr(self, val) -> str:
        return binascii.b2a_hex(val).decode("utf-8")

    async def _readUUID(self, uuid) -> bytearray:
        val = await self._dev.read_gatt_char(uuid)
        return val

    async def _readHandle(self, handle) -> bytearray:
        val = await self._dev.read_gatt_char(char_specifier=handle)
        return val

    async def _writeUUID(self, uuid, val) -> None:
        await self._dev.write_gatt_char(char_specifier=uuid, data=val, response=True)

    # --- Generic GATT Characteristics

    async def getDeviceName(self) -> str:
        # Why does UUID "0x2" fail here? Doesn't when testing from my PC...
        return (await self._readHandle(self.device.chars[CHARACTERISTIC_MODEL_NAME])).decode("ascii")

    async def getModelNumber(self) -> str:
        return (await self._readHandle(0xD)).decode("ascii")

    async def getSerialNumber(self) -> str:
        return (await self._readHandle(0xB)).decode("ascii")

    async def getHardwareRevision(self) -> str:
        return (await self._readHandle(0xF)).decode("ascii")

    async def getFirmwareRevision(self) -> str:
        return (await self._readHandle(0x11)).decode("ascii")

    async def getSoftwareRevision(self) -> str:
        return (await self._readHandle(0x13)).decode("ascii")

    async def getManufacturer(self) -> str:
        return (await self._readHandle(0x15)).decode("ascii")

    # --- Onwards to PAX characteristics

    async def setAuth(self, pin) -> None:
        await self._writeUUID(self.chars.CHARACTERISTIC_PIN_CODE, pack("<I", int(pin)))

    async def checkAuth(self) -> bool:
        v = unpack("<b", await self._readUUID(self.device.chars[CHARACTERISTIC_PIN_CONFIRMATION]))
        return bool(v[0])

    async def setAlias(self, name) -> None:
        await self._writeUUID(
            self.device.chars[CHARACTERISTIC_FAN_DESCRIPTION], pack("20s", bytearray(name, "utf-8"))
        )

    async def getAlias(self)-> str:
        return await self._readUUID(self.device.chars[CHARACTERISTIC_FAN_DESCRIPTION]).decode("utf-8")

    async def getIsClockSet(self) -> str:
        return self._bToStr(await self._readUUID(self.device.chars[CHARACTERISTIC_STATUS]))

    async def getState(self) -> FanState:
        # Short Short Short Short    Byte Short Byte
        # Hum   Temp  Light FanSpeed Mode Tbd   Tbd
        v = unpack("<4HBHB", await self._readUUID(self.device.chars[CHARACTERISTIC_SENSOR_DATA]))
        _LOGGER.debug("Read Fan States: %s", v)

        trigger = "No trigger"
        if ((v[4] >> 4) & 1) == 1:
            trigger = "Boost"
        elif ((v[4] >> 6) & 3) == 3:
            trigger = "Switch"
        elif (v[4] & 3) == 1:
            trigger = "Trickle ventilation"
        elif (v[4] & 3) == 2:
            trigger = "Light ventilation"
        elif (
            v[4] & 3
        ) == 3:  # Note that the trigger might be active, but mode must be enabled to be activated
            trigger = "Humidity ventilation"

        return FanState(
            round(math.log2(v[0] - 30) * 10, 2) if v[0] > 30 else 0,
            v[1] / 4 - 2.6,
            v[2],
            v[3],
            trigger,
        )

    async def getFactorySettingsChanged(self) -> bool:
        v = unpack("<?", await self._readUUID(self.device.chars[CHARACTERISTIC_FACTORY_SETTINGS_CHANGED]))
        return v[0]

    async def getMode(self) -> str:
        v = unpack("<B", await self._readUUID(self.device.chars[CHARACTERISTIC_MODE]))
        if v[0] == 0:
            return "MultiMode"
        elif v[0] == 1:
            return "DraftShutterMode"
        elif v[0] == 2:
            return "WallSwitchExtendedRuntimeMode"
        elif v[0] == 3:
            return "WallSwitchNoExtendedRuntimeMode"
        elif v[0] == 4:
            return "HeatDistributionMode"

    async def setFanSpeedSettings(self, humidity=2250, light=1625, trickle=1000) -> None:
        for val in (humidity, light, trickle):
            if val % 25 != 0:
                raise ValueError("Speeds should be multiples of 25")
            if val > 2500 or val < 0:
                raise ValueError("Speeds must be between 0 and 2500 rpm")

        _LOGGER.debug("Calima setFanSpeedSettings: %s %s %s", humidity, light, trickle)

        await self._writeUUID(
            self.device.chars[CHARACTERISTIC_LEVEL_OF_FAN_SPEED], pack("<HHH", humidity, light, trickle)
        )

    async def getFanSpeedSettings(self) -> Fanspeeds:
        return Fanspeeds._make(
            unpack("<HHH", await self._readUUID(self.device.chars[CHARACTERISTIC_LEVEL_OF_FAN_SPEED]))
        )

    async def setSensorsSensitivity(self, humidity, light) -> None:
        if humidity > 3 or humidity < 0:
            raise ValueError("Humidity sensitivity must be between 0-3")
        if light > 3 or light < 0:
            raise ValueError("Light sensitivity must be between 0-3")

        value = pack("<4B", bool(humidity), humidity, bool(light), light)
        await self._writeUUID(CHARACTERISTIC_SENSITIVITY, value)

    async def getSensorsSensitivity(self) -> Sensitivity:
        # Hum Active | Hum Sensitivity | Light Active | Light Sensitivity
        # We fix so that Sensitivity = 0 if active = 0
        l = Sensitivity._make(
            unpack("<4B", await self._readUUID(CHARACTERISTIC_SENSITIVITY))
        )

        return Sensitivity._make(
            unpack(
                "<4B",
                bytearray(
                    [
                        l.HumidityOn,
                        l.HumidityOn and l.Humidity,
                        l.LightOn,
                        l.LightOn and l.Light,
                    ]
                ),
            )
        )

    async def setLightSensorSettings(self, delayed, running) -> None:
        if delayed not in (0, 5, 10):
            raise ValueError("Delayed must be 0, 5 or 10 minutes")
        if running not in (5, 10, 15, 30, 60):
            raise ValueError("Running time must be 5, 10, 15, 30 or 60 minutes")

        await self._writeUUID(
            CHARACTERISTIC_TIME_FUNCTIONS, pack("<2B", delayed, running)
        )

    async def getLightSensorSettings(self) -> LightSensorSettings:
        return LightSensorSettings._make(
            unpack("<2B", await self._readUUID(CHARACTERISTIC_TIME_FUNCTIONS))
        )

    async def getHeatDistributor(self) -> HeatDistributorSettings:
        return HeatDistributorSettings._make(
            unpack("<BHH", await self._readUUID(CHARACTERISTIC_TEMP_HEAT_DISTRIBUTOR))
        )

    async def setBoostMode(self, on, speed, seconds) -> None:
        if speed % 25:
            raise ValueError("Speed must be a multiple of 25")
        if not on:
            speed = 0
            seconds = 0

        await self._writeUUID(self.device.chars[CHARACTERISTIC_BOOST], pack("<BHH", on, speed, seconds))

    async def getBoostMode(self) -> BoostMode:
        v = unpack("<BHH", await self._readUUID(self.device.chars[CHARACTERISTIC_BOOST]))
        return BoostMode._make(v)

    async def getLed(self) -> str:
        return self._bToStr(await self._readUUID(self.device.chars[CHARACTERISTIC_LED]))

    async def setAutomaticCycles(self, setting: int) -> None:
        if setting < 0 or setting > 3:
            raise ValueError("Setting must be between 0-3")

        await self._writeUUID(self.device.chars[CHARACTERISTIC_AUTOMATIC_CYCLES], pack("<B", setting))

    async def getAutomaticCycles(self) -> int:
        v = unpack("<B", await self._readUUID(self.device.chars[CHARACTERISTIC_AUTOMATIC_CYCLES]))
        return v[0]

    async def setTime(self, dayofweek, hour, minute, second) -> None:
        await self._writeUUID(
            self.device.chars[CHARACTERISTIC_CLOCK], pack("<4B", dayofweek, hour, minute, second)
        )

    async def getTime(self):
        return Time._make(unpack("<BBBB", await self._readUUID(self.device.chars[CHARACTERISTIC_CLOCK])))

    async def setTimeToNow(self) -> None:
        now = datetime.datetime.now()
        await self.setTime(now.isoweekday(), now.hour, now.minute, now.second)

    async def setSilentHours(self, on: bool, startingTime: datetime.time, endingTime: datetime.time) -> None:
        _LOGGER.debug("Writing silent hours %s %s %s %s", startingTime.hour, startingTime.minute, endingTime.hour, endingTime.minute)
        value = pack(
            "<5B", int(on), startingTime.hour, startingTime.minute, endingTime.hour, endingTime.minute
        )
        await self._writeUUID(self.device.chars[CHARACTERISTIC_NIGHT_MODE], value)

    async def getSilentHours(self) -> SilentHours:
        return SilentHours._make(
            unpack("<5B", await self._readUUID(self.device.chars[CHARACTERISTIC_NIGHT_MODE]))
        )

    async def setTrickleDays(self, weekdays, weekends) -> None:
        await self._writeUUID(
            self.device.chars[CHARACTERISTIC_BASIC_VENTILATION], pack("<2B", weekdays, weekends)
        )

    async def getTrickleDays(self) -> TrickleDays:
        return TrickleDays._make(
            unpack("<2B", await self._readUUID(self.device.chars[CHARACTERISTIC_BASIC_VENTILATION]))
        )

    async def getReset(self):  # Should be write
        return await self._readUUID(    self.device.chars[CHARACTERISTIC_RESET])

    async def resetDevice(self):  # Dangerous
        await self._writeUUID(self.device.chars[CHARACTERISTIC_RESET], pack("<I", 120))

    async def resetValues(self):  # Danguerous
        await self._writeUUID(self.device.chars[CHARACTERISTIC_RESET], pack("<I", 85))
