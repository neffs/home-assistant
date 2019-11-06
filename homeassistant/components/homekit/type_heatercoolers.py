"""Class to hold all thermostat accessories."""
import logging

from pyhap.const import CATEGORY_THERMOSTAT

from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTIONS,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DOMAIN as DOMAIN_CLIMATE,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_AUTO,
    HVAC_MODE_OFF,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_DRY,
    SERVICE_SET_HVAC_MODE as SERVICE_SET_HVAC_MODE_THERMOSTAT,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE as SERVICE_SET_TEMPERATURE_THERMOSTAT,
    SUPPORT_TARGET_TEMPERATURE_RANGE,
    SUPPORT_TARGET_TEMPERATURE,
    SUPPORT_FAN_MODE,
    SUPPORT_SWING_MODE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
)

from . import TYPES
from .accessories import HomeAccessory, debounce
from .const import (
    CHAR_ACTIVE,
    CHAR_COOLING_THRESHOLD_TEMPERATURE,
    CHAR_CURRENT_HEATER_COOLER,
    CHAR_CURRENT_TEMPERATURE,
    CHAR_HEATING_THRESHOLD_TEMPERATURE,
    CHAR_TARGET_HEATER_COOLER,
    CHAR_TEMP_DISPLAY_UNITS,
    CHAR_ROTATION_SPEED,
    CHAR_SWING_MODE,
    PROP_MAX_VALUE,
    PROP_MIN_STEP,
    PROP_MIN_VALUE,
    SERV_HEATER_COOLER,
)
from .util import temperature_to_homekit, temperature_to_states, HomeKitSpeedMapping


_LOGGER = logging.getLogger(__name__)

UNIT_HASS_TO_HOMEKIT = {TEMP_CELSIUS: 0, TEMP_FAHRENHEIT: 1}
UNIT_HOMEKIT_TO_HASS = {c: s for s, c in UNIT_HASS_TO_HOMEKIT.items()}
HC_HASS_TO_HOMEKIT = {
    HVAC_MODE_HEAT: 1,
    HVAC_MODE_COOL: 2,
    HVAC_MODE_AUTO: 0,
    HVAC_MODE_HEAT_COOL: 0,
    HVAC_MODE_FAN_ONLY: 2,
}

HC_HASS_TO_HOMEKIT_ACTION = {
    CURRENT_HVAC_OFF: 0,
    CURRENT_HVAC_IDLE: 1,
    CURRENT_HVAC_HEAT: 2,
    CURRENT_HVAC_COOL: 3,
}
ACTIVE_HASS_TO_HOMEKIT = {HVAC_MODE_OFF: 0, TEMP_FAHRENHEIT: 1}


@TYPES.register("HeaterCooler")
class HeaterCooler(HomeAccessory):
    """Generate a HeaterCooler accessory for a climate."""

    def __init__(self, *args):
        """Initialize a Thermostat accessory object."""
        super().__init__(*args, category=CATEGORY_THERMOSTAT)
        self._unit = self.hass.config.units.temperature_unit
        self._flag_heat_cool = False
        self._flag_temperature = False
        self._flag_coolingthresh = False
        self._flag_heatingthresh = False
        min_temp, max_temp = self.get_temperature_range()
        temp_step = self.hass.states.get(self.entity_id).attributes.get(
            ATTR_TARGET_TEMP_STEP, 0.5
        )
        single_temperature_char = None

        # Add additional characteristics
        self.chars = [
            CHAR_TEMP_DISPLAY_UNITS,
        ]
        state = self.hass.states.get(self.entity_id)
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        modes = state.attributes.get(ATTR_HVAC_MODES, None)
        if modes is None:
            _LOGGER.error(
                "%s: HVAC modes not yet available for this entity. We can't determine if this device is a heater/cooler or both. Please delay the HomeKit start!",
                self.entity_id,
            )
            modes = list(HVAC_MODE_HEAT, HVAC_MODE_COOL, HVAC_MODE_HEAT_COOL)

        if features & SUPPORT_TARGET_TEMPERATURE_RANGE:
            self.chars.extend(
                (CHAR_COOLING_THRESHOLD_TEMPERATURE, CHAR_HEATING_THRESHOLD_TEMPERATURE)
            )
        elif features & SUPPORT_TARGET_TEMPERATURE:
            # there is no single target temperature equivalent in Homekit HeaterCooler
            # We'll use the cooling temperature for cooling-only devices and heating
            if HVAC_MODE_COOL in modes and HVAC_MODE_HEAT in modes:
                single_temperature_char = CHAR_HEATING_THRESHOLD_TEMPERATURE
                _LOGGER.error(
                    "%s: Entities supporting heating and cooling should use a temperature range. Home Assistant's target temperature is mapped to HomeKit's heating target.",
                    self.entity_id,
                )
            elif HVAC_MODE_COOL in modes:
                single_temperature_char = CHAR_COOLING_THRESHOLD_TEMPERATURE
            else:
                single_temperature_char = CHAR_HEATING_THRESHOLD_TEMPERATURE
            self.chars.append(single_temperature_char)

        if features & SUPPORT_FAN_MODE:
            speed_list = state.attributes.get(ATTR_FAN_MODES)
            self.speed_mapping = HomeKitSpeedMapping(speed_list)
            self.chars.append(CHAR_ROTATION_SPEED)

        if features & SUPPORT_SWING_MODE:
            swing_modes = state.attributes.get(ATTR_SWING_MODES, list())
            swing_off = [a for a in swing_modes if "off" in a.lower()]
            swing_on = [a for a in swing_modes if a not in swing_off]
            if len(swing_on) > 0 and len(swing_off) > 0:
                self._swing_off = swing_off[0]
                self._swing_on = swing_on[0]
                self.chars.append(CHAR_SWING_MODE)
            else:
                _LOGGER.error("%s: Could not map swing modes, ", self.entity_id)

        serv_thermostat = self.add_preload_service(SERV_HEATER_COOLER, self.chars)

        # Current and target mode characteristics
        self.char_current_heat_cool = serv_thermostat.configure_char(
            CHAR_CURRENT_HEATER_COOLER, value=0
        )

        # determine available modes
        usable_modes = [a for a in modes if a in HC_HASS_TO_HOMEKIT.keys()]
        if HVAC_MODE_AUTO in usable_modes and HVAC_MODE_HEAT_COOL in usable_modes:
            usable_modes.remove(HVAC_MODE_HEAT_COOL)
        if HVAC_MODE_COOL in usable_modes and HVAC_MODE_FAN_ONLY in usable_modes:
            usable_modes.remove(HVAC_MODE_FAN_ONLY)

        self.hc_homekit_to_hass = {
            c: s for s, c in HC_HASS_TO_HOMEKIT.items() if s in usable_modes
        }

        valid_modes = (("Auto", 0), ("Cool", 2), ("Heat", 1))
        valid_values = {k: v for k, v in valid_modes if v in self.hc_homekit_to_hass}

        self.char_target_heat_cool = serv_thermostat.configure_char(
            CHAR_TARGET_HEATER_COOLER,
            setter_callback=self.set_heat_cool,
            valid_values=valid_values,
        )

        self.char_active = serv_thermostat.configure_char(
            CHAR_ACTIVE, value=1, setter_callback=self.set_active
        )

        # Current and target temperature characteristics
        self.char_current_temp = serv_thermostat.configure_char(
            CHAR_CURRENT_TEMPERATURE, value=21.0
        )
        # Display units characteristic
        self.char_display_units = serv_thermostat.configure_char(
            CHAR_TEMP_DISPLAY_UNITS, value=0
        )
        self.char_target_temp = None
        self.char_cooling_thresh_temp = None
        self.char_heating_thresh_temp = None
        if features & SUPPORT_TARGET_TEMPERATURE_RANGE:
            self.char_cooling_thresh_temp = serv_thermostat.configure_char(
                CHAR_COOLING_THRESHOLD_TEMPERATURE,
                value=23.0,
                properties={
                    PROP_MIN_VALUE: min_temp,
                    PROP_MAX_VALUE: max_temp,
                    PROP_MIN_STEP: temp_step,
                },
                setter_callback=self.set_cooling_threshold,
            )
            self.char_heating_thresh_temp = serv_thermostat.configure_char(
                CHAR_HEATING_THRESHOLD_TEMPERATURE,
                value=19.0,
                properties={
                    PROP_MIN_VALUE: min_temp,
                    PROP_MAX_VALUE: max_temp,
                    PROP_MIN_STEP: temp_step,
                },
                setter_callback=self.set_heating_threshold,
            )
        elif features & SUPPORT_TARGET_TEMPERATURE:
            self.char_target_temp = serv_thermostat.configure_char(
                single_temperature_char,
                value=21.0,
                properties={
                    PROP_MIN_VALUE: min_temp,
                    PROP_MAX_VALUE: max_temp,
                    PROP_MIN_STEP: temp_step,
                },
                setter_callback=self.set_target_temperature,
            )
        if CHAR_ROTATION_SPEED in self.chars:
            self.char_speed = serv_thermostat.configure_char(
                CHAR_ROTATION_SPEED, value=0, setter_callback=self.set_fan_mode
            )

        if CHAR_SWING_MODE in self.chars:
            self.char_swing = serv_thermostat.configure_char(
                CHAR_SWING_MODE, value=0, setter_callback=self.set_swing_mode
            )

    def get_temperature_range(self):
        """Return min and max temperature range."""
        max_temp = self.hass.states.get(self.entity_id).attributes.get(ATTR_MAX_TEMP)
        max_temp = (
            temperature_to_homekit(max_temp, self._unit)
            if max_temp
            else DEFAULT_MAX_TEMP
        )
        max_temp = round(max_temp * 2) / 2

        min_temp = self.hass.states.get(self.entity_id).attributes.get(ATTR_MIN_TEMP)
        min_temp = (
            temperature_to_homekit(min_temp, self._unit)
            if min_temp
            else DEFAULT_MIN_TEMP
        )
        min_temp = round(min_temp * 2) / 2

        return min_temp, max_temp

    def set_heat_cool(self, value):
        """Change operation mode to value if call came from HomeKit."""
        is_active = self.char_active.get_value()
        if value in self.hc_homekit_to_hass:
            if is_active:
                self._flag_heat_cool = True
                hass_value = self.hc_homekit_to_hass[value]
                _LOGGER.debug(
                    "%s: Set heat-cool to %d - %s", self.entity_id, value, hass_value
                )
                params = {ATTR_ENTITY_ID: self.entity_id, ATTR_HVAC_MODE: hass_value}
                self.call_service(
                    DOMAIN_CLIMATE, SERVICE_SET_HVAC_MODE_THERMOSTAT, params, hass_value
                )
            else:
                _LOGGER.debug(
                    "%s: updated heat-cool characteristic, but service is inactive %d ",
                    self.entity_id,
                    value,
                )
        else:
            _LOGGER.error(
                "%s: HomeKit tried to set invalid heat-cool characteristic: %d ",
                self.entity_id,
                value,
            )

    def set_active(self, value):
        """Change operation mode to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set active to %d", self.entity_id, value)
        # TODO: Ignore active==1 for now, because HomeKit will provide a new mode anyway.
        if value == 0:
            hass_value = HVAC_MODE_OFF
            self._flag_heat_cool = True
            params = {ATTR_ENTITY_ID: self.entity_id, ATTR_HVAC_MODE: hass_value}
            self.call_service(
                DOMAIN_CLIMATE, SERVICE_SET_HVAC_MODE_THERMOSTAT, params, hass_value
            )
        else:
            current_hk_mode = self.char_target_heat_cool.get_value()
            self.set_heat_cool(current_hk_mode)

    @debounce
    def set_cooling_threshold(self, value):
        """Set cooling threshold temp to value if call came from HomeKit."""
        _LOGGER.debug(
            "%s: Set cooling threshold temperature to %.1f°C", self.entity_id, value
        )
        self._flag_coolingthresh = True
        low = self.char_heating_thresh_temp.value
        temperature = temperature_to_states(value, self._unit)
        params = {
            ATTR_ENTITY_ID: self.entity_id,
            ATTR_TARGET_TEMP_HIGH: temperature,
            ATTR_TARGET_TEMP_LOW: temperature_to_states(low, self._unit),
        }
        self.call_service(
            DOMAIN_CLIMATE,
            SERVICE_SET_TEMPERATURE_THERMOSTAT,
            params,
            f"cooling threshold {temperature}{self._unit}",
        )

    @debounce
    def set_heating_threshold(self, value):
        """Set heating threshold temp to value if call came from HomeKit."""
        _LOGGER.debug(
            "%s: Set heating threshold temperature to %.1f°C", self.entity_id, value
        )
        self._flag_heatingthresh = True
        high = self.char_cooling_thresh_temp.value
        temperature = temperature_to_states(value, self._unit)
        params = {
            ATTR_ENTITY_ID: self.entity_id,
            ATTR_TARGET_TEMP_HIGH: temperature_to_states(high, self._unit),
            ATTR_TARGET_TEMP_LOW: temperature,
        }
        self.call_service(
            DOMAIN_CLIMATE,
            SERVICE_SET_TEMPERATURE_THERMOSTAT,
            params,
            f"heating threshold {temperature}{self._unit}",
        )

    @debounce
    def set_target_temperature(self, value):
        """Set target temperature to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set target temperature to %.1f°C", self.entity_id, value)
        self._flag_temperature = True
        temperature = temperature_to_states(value, self._unit)
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_TEMPERATURE: temperature}
        self.call_service(
            DOMAIN_CLIMATE,
            SERVICE_SET_TEMPERATURE_THERMOSTAT,
            params,
            f"{temperature}{self._unit}",
        )

    def set_swing_mode(self, value):
        """Set state if call came from HomeKit."""
        _LOGGER.debug("%s: Set oscillating to %d", self.entity_id, value)
        self._flag_swing = True

        oscillating = self._swing_on if value == 1 else self._swing_off
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_SWING_MODE: oscillating}  # TODO
        self.call_service(DOMAIN_CLIMATE, SERVICE_SET_SWING_MODE, params, oscillating)

    @debounce
    def set_fan_mode(self, value):
        """Set state if call came from HomeKit."""
        self._flag_fan = True
        _LOGGER.debug("%s: Set speed to %d", self.entity_id, value)
        speed = self.speed_mapping.speed_to_states(value)
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_FAN_MODE: speed}
        self.call_service(DOMAIN_CLIMATE, SERVICE_SET_FAN_MODE, params, speed)

    def update_state(self, new_state):
        """Update thermostat state after state changed."""
        # Update current temperature
        current_temp = new_state.attributes.get(ATTR_CURRENT_TEMPERATURE)
        if isinstance(current_temp, (int, float)):
            current_temp = temperature_to_homekit(current_temp, self._unit)
            self.char_current_temp.set_value(current_temp)

        # Update target temperature
        if self.char_target_temp:
            target_temp = new_state.attributes.get(ATTR_TEMPERATURE)
            if isinstance(target_temp, (int, float)):
                target_temp = temperature_to_homekit(target_temp, self._unit)
                if not self._flag_temperature:
                    self.char_target_temp.set_value(target_temp)
            self._flag_temperature = False

        # Update cooling threshold temperature if characteristic exists
        if self.char_cooling_thresh_temp:
            cooling_thresh = new_state.attributes.get(ATTR_TARGET_TEMP_HIGH)
            if isinstance(cooling_thresh, (int, float)):
                cooling_thresh = temperature_to_homekit(cooling_thresh, self._unit)
                if not self._flag_coolingthresh:
                    self.char_cooling_thresh_temp.set_value(cooling_thresh)
        self._flag_coolingthresh = False

        # Update heating threshold temperature if characteristic exists
        if self.char_heating_thresh_temp:
            heating_thresh = new_state.attributes.get(ATTR_TARGET_TEMP_LOW)
            if isinstance(heating_thresh, (int, float)):
                heating_thresh = temperature_to_homekit(heating_thresh, self._unit)
                if not self._flag_heatingthresh:
                    self.char_heating_thresh_temp.set_value(heating_thresh)
        self._flag_heatingthresh = False

        # Update display units
        if self._unit and self._unit in UNIT_HASS_TO_HOMEKIT:
            self.char_display_units.set_value(UNIT_HASS_TO_HOMEKIT[self._unit])

        # Update target operation mode
        hvac_mode = new_state.state
        if hvac_mode and hvac_mode in (HVAC_MODE_OFF, HVAC_MODE_DRY):
            if not self._flag_heat_cool:
                self.char_active.set_value(0)
        elif hvac_mode and hvac_mode in HC_HASS_TO_HOMEKIT:
            if not self._flag_heat_cool:
                self.char_active.set_value(1)
                self.char_target_heat_cool.set_value(HC_HASS_TO_HOMEKIT[hvac_mode])
        self._flag_heat_cool = False

        # Set current operation mode for supported thermostats
        hvac_action = new_state.attributes.get(ATTR_HVAC_ACTIONS)
        if hvac_action:
            self.char_current_heat_cool.set_value(
                HC_HASS_TO_HOMEKIT_ACTION[hvac_action]
            )
        # Handle Speed
        if self.char_speed:
            speed = new_state.attributes.get(ATTR_FAN_MODE)
            hk_speed_value = self.speed_mapping.speed_to_homekit(speed)
            if hk_speed_value is not None and self.char_speed.value != hk_speed_value:
                self.char_speed.set_value(hk_speed_value)
            self._flag_fan = False

        # Handle Oscillating
        if self.char_swing:
            oscillating = new_state.attributes.get(ATTR_SWING_MODE)
            if not self._flag_swing:
                hk_oscillating = 0 if "off" in oscillating.lower() else 1
                self.char_swing.set_value(hk_oscillating)
            self._flag_swing = False
