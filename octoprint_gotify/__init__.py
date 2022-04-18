# coding=utf-8
from __future__ import absolute_import, division, print_function, unicode_literals

import os
from typing import Optional
import flask
import json
import octoprint.plugin
import octoprint.plugin
import requests
from requests.exceptions import HTTPError
import datetime
import octoprint.util
from io import StringIO, BytesIO
from PIL import Image
from flask_login import current_user
from octoprint.util import RepeatedTimer

__author__ = "Alwin Lohrie <alwin@cloudserver.click>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Released under terms of the AGPLv3 License"
__plugin_name__ = "Gotify"
__plugin_pythoncompat__ = ">=2.7,<4"


class GotifyPlugin(octoprint.plugin.EventHandlerPlugin,
                   octoprint.plugin.SettingsPlugin,
                   octoprint.plugin.StartupPlugin,
                   octoprint.plugin.SimpleApiPlugin,
                   octoprint.plugin.TemplatePlugin,
                   octoprint.plugin.AssetPlugin,
                   octoprint.plugin.ProgressPlugin,
                   octoprint.plugin.OctoPrintPlugin):
    m70_cmd = ""
    printing = False
    start_time = None
    last_minute = 0
    last_progress = 0
    first_layer = False
    timer = None
    bed_sent = False
    e1_sent = False
    progress = 0
    emoji = {
        'rocket': u'\U0001F680',
        'clock': u'\U000023F0',
        'warning': u'\U000026A0',
        'finish': u'\U0001F3C1',
        'hooray': u'\U0001F389',
        'error': u'\U000026D4',
        'stop': u'\U000025FC',
        'temp': u'\U0001F321',
        'four_leaf_clover': u'\U0001f340',
        'waving_hand_sign': u'\U0001f44b',
    }

    def get_emoji(self, key):
        if key in self.emoji:
            return self.emoji[key]
        return ""

    def get_assets(self):
        return {
            "js": ["js/gotify.js"]
        }

    def get_api_commands(self):
        return dict(
            test=["app_token"]
        )

    def on_api_command(self, command, data):
        if command == "test":
            self._logger.debug("sending gotify test message")
            if not data["app_token"]:
                data["app_token"] = self.get_token()

            # When we are testing the token, create a test notification
            payload = {
                "title": "OctoPrint push test",
                "message": u''.join([u"pewpewpew!! OctoPrint works. ", self.get_emoji("rocket")]),
            }

            # Validate the user key and send a message
            try:
                self.event_message(payload)
                return flask.jsonify(dict(success=True))
            except Exception as e:
                return flask.jsonify(dict(success=False, msg=str(e.message)))
        return flask.make_response("Unknown command", 400)

    def image(self) -> Optional[bytes]:
        """
        Create an image by getting an image form the setting webcam-snapshot. 
        Transpose this image according the settings and returns it 
        :return: 
        """
        snapshot_url = self._settings.global_get(["webcam", "snapshot"])
        if not snapshot_url:
            return None

        self._logger.debug("Snapshot URL: %s " % str(snapshot_url))
        try:
            image = requests.get(snapshot_url, stream=True).content
        except HTTPError as http_err:
            self._logger.info(
                "HTTP error occured while trying to get image: %s " % str(http_err))
        except Exception as err:
            self._logger.info(
                "Other error occurred while trying to get image: %s " % str(err))

        hflip = self._settings.global_get(["webcam", "flipH"])
        vflip = self._settings.global_get(["webcam", "flipV"])
        rotate = self._settings.global_get(["webcam", "rotate90"])
        if hflip or vflip or rotate:
            # https://www.blog.pythonlibrary.org/2017/10/05/how-to-rotate-mirror-photos-with-python/
            image_obj = Image.open(BytesIO(image))
            if hflip:
                image_obj = image_obj.transpose(Image.FLIP_LEFT_RIGHT)
            if vflip:
                image_obj = image_obj.transpose(Image.FLIP_TOP_BOTTOM)
            if rotate:
                image_obj = image_obj.rotate(90)
            # https://stackoverflow.com/questions/646286/python-pil-how-to-write-png-image-to-string/5504072
            output = BytesIO()
            image_obj.save(output, format="JPEG")
            image = output.getvalue()
            output.close()

        return image

    def restart_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

        self.timer = RepeatedTimer(5, self.temp_check, None, None, True)
        self.timer.start()

    def temp_check(self):
        """
        called repeadly by Timer. Sends temperature push
        """

        if not self._printer.is_operational():
            return

        if self._settings.get(["events", "TempReached", "priority"]):

            temps = self._printer.get_current_temperatures()

            bed_temp = round(temps['bed']['actual']) if 'bed' in temps else 0
            bed_target = temps['bed']['target'] if 'bed' in temps else 0
            e1_temp = round(temps['tool0']['actual']
                            ) if 'tool0' in temps else 0
            e1_target = temps['tool0']['target'] if 'tool0' in temps else 0

            if bed_target > 0 and bed_temp >= bed_target and self.bed_sent is False:
                self.bed_sent = True

                self.event_message({
                    "message": self._settings.get(["events", "TempReached", "message"]).format(**locals())
                })

            if e1_target > 0 and e1_temp >= e1_target and self.e1_sent is False:
                self.e1_sent = True

                self.event_message({
                    "message": self._settings.get(["events", "TempReached", "message"]).format(**locals())
                })

    def on_print_progress(self, storage: str, path: str, progress: int):
        """
        Called by OctoPrint on minimally 1% increments during a running print job.

        :param string storage: Location of the file
        :param string path: Path of the file
        :param int progress: Current progress as a value between 0 and 100
        """
        progressMod = self._settings.get(["events", "Progress", "mod"])

        if self.printing and progressMod and progress > 0 and progress % int(progressMod) == 0 and self.last_progress != progress:
            self.last_progress = progress
            self.event_message({
                "message": self._settings.get(["events", "Progress", "message"]).format(percentage=progress),
                "priority": self._settings.get(["events", "Scheduled", "priority"])
            })

    def get_mins_since_started(self) -> int:
        if self.start_time:
            return int(round((datetime.datetime.now() - self.start_time).total_seconds() / 60, 0))

    def check_schedule(self):
        """
        Check the scheduler
        Send a notification
        """

        scheduleMod = self._settings.get(["events", "Scheduled", "mod"])

        if self.printing and scheduleMod and self.last_minute > 0 and self.last_minute % int(scheduleMod) == 0:

            self.event_message({
                "message": self._settings.get(["events", "Scheduled", "message"]).format(elapsed_time=self.last_minute),
                "priority": self._settings.get(["events", "Scheduled", "priority"])
            })

    def sent_gcode(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        """
        M70 Gcode commands are used for sending a text when print is paused
        :param comm_instance: 
        :param phase: 
        :param cmd: 
        :param cmd_type: 
        :param gcode: 
        :param args: 
        :param kwargs: 
        :return: 
        """

        if gcode and gcode != "G1":
            mss = self.get_mins_since_started()

            if self.last_minute != mss:
                self.last_minute = mss
                self.check_schedule()

        if gcode and gcode == "M600":
            self.on_event("FilamentChange", None)

        if gcode and gcode == "M70":
            self.m70_cmd = cmd[3:]

        if gcode and gcode == "M117" and cmd[4:].strip() != "":
            self.m70_cmd = cmd[4:]

    # Start with event handling: http://docs.octoprint.org/en/master/events/index.html

    def PrintDone(self, payload):
        """
        When the print is done, enhance the payload with the filename and the elapsed time and return it 
        :param payload: 
        :return: 
        """
        self.printing = False
        self.last_minute = 0
        self.last_progress = 0
        self.start_time = None
        file = os.path.basename(payload["name"])
        elapsed_time_in_seconds = payload["time"]

        elapsed_time = octoprint.util.get_formatted_timedelta(
            datetime.timedelta(seconds=elapsed_time_in_seconds))

        # Create the message
        return self._settings.get(["events", "PrintDone", "message"]).format(**locals())

    def PrintFailed(self, payload):
        """
        When the print is failed, enhance the payload with the filename and returns it 
        :param payload: 
        :return: 
        """
        self.printing = False
        if "name" in payload:
            file = os.path.basename(payload["name"])
        return self._settings.get(["events", "PrintFailed", "message"]).format(**locals())

    def FilamentChange(self, payload):
        """
        When a M600 command is received the user is asked to change the filament
        :param payload: 
        :return: 
        """
        m70_cmd = ""
        if (self.m70_cmd != ""):
            m70_cmd = "(" + self.m70_cmd.strip() + ")"

        return self._settings.get(["events", "FilamentChange", "message"]).format(**locals())

    def PrintPaused(self, payload):
        """
        When the print is paused check if there is a m70 command, and replace that in the message.
        :param payload: 
        :return: 
        """
        m70_cmd = ""
        if (self.m70_cmd != ""):
            m70_cmd = self.m70_cmd

        return self._settings.get(["events", "PrintPaused", "message"]).format(**locals())

    def Waiting(self, payload):
        """
        Alias for PrintPaused
        :param payload: 
        :return: 
        """
        return self.PrintPaused(payload)

    def PrintStarted(self, payload):
        """
        Reset value's
        :param payload:
        :return:
        """

        self.printing = True
        self.start_time = datetime.datetime.now()
        self.m70_cmd = ""
        self.bed_sent = False
        self.e1_sent = False
        self.first_layer = True
        self.restart_timer()

        return self._settings.get(["events", "PrintStarted", "message"])

    def ZChange(self, payload):
        """
        ZChange event which send a notification, this does not work when printing from sd
        :param payload: 
        :return: 
        """

        if not self.printing:
            return

        if not self.first_layer:
            return

        # It is not actually the first layer, it was not my plan too create a lot of code for this feature
        if payload["new"] < 2 or payload["old"] is None:
            return

        self.first_layer = False
        return self._settings.get(["events", "ZChange", "message"]).format(**locals())

    def Startup(self, payload):
        """
        Event triggered when printer is started up
        :param payload: 
        :return: 
        """

        return self._settings.get(["events", "Startup", "message"])

    def Shutdown(self, payload):
        """
        PrinterShutdown
        :param payload: 
        :return: 
        """
        return self._settings.get(["events", "Shutdown", "message"])

    def Error(self, payload):
        """
        Only continue when the current state is printing
        :param payload: 
        :return: 
        """
        if(self.printing):
            error = payload["error"]
            return self._settings.get(["events", "Error", "message"]).format(**locals())
        return

    def on_event(self, event, payload):

        if payload is None:
            payload = {}

        # StatusNotPrinting
        self._logger.debug("Got an event: %s, payload: %s" %
                           (event, str(payload)))
        # It's easier to ask forgiveness than to ask permission.
        try:
            # Method exists, and was used.
            payload["message"] = getattr(self, event)(payload)

            self._logger.debug("Event triggered: %s " % str(event))
        except AttributeError:
            self._logger.debug(
                "event: %s has an AttributeError %s" % (event, str(payload)))
            # By default the message is simple and does not need any formatting
            payload["message"] = self._settings.get(
                ["events", event, "message"])

        if payload["message"] is None:
            self._logger.debug("no message in payload")
            return

        # Does the event exists in the settings ? if not we don't want it
        if not event in self.get_settings_defaults()["events"]:
            self._logger.debug("we don't like that event")
            return

        # Only continue when there is a priority
        priority = self._settings.get(["events", event, "priority"])

        # By default, messages have normal priority (a priority of 0).
        # We do not support the Emergency Priority (2) because there is no way of canceling it here,
        if priority:
            payload["priority"] = priority
            self.event_message(payload)

    def event_message(self, payload):
        """
        Do send the notification to the gotify server :)
        :param payload: 
        :return: 
        """

        if payload.get("priority") and isinstance(payload.get("priority"), str):
            payload['priority'] = int(payload.get('priority'))

        if self._printer_profile_manager is not None and "name" in self._printer_profile_manager.get_current_or_default():
            payload["title"] = "Octoprint: %s" % self._printer_profile_manager.get_current_or_default()[
                "name"]

        try:
            r = requests.post(
                f"{self._settings.get(['gotify_server_base_url'])}/message?token={self.get_token()}", json=payload)
            self._logger.debug("Response: %s" % str(r.content))
        except Exception as e:
            self._logger.info("Could not send message: %s" % str(e))

    def on_after_startup(self):
        """
        Valide settings on startup
        :return: 
        """

        self.restart_timer()

    def get_settings_version(self):
        return 1

    def on_settings_save(self, data):
        """
        Valide settings on save
        :param data: 
        :return: 
        """
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        self.restart_timer()

    def on_settings_load(self):
        data = octoprint.plugin.SettingsPlugin.on_settings_load(self)

        # only return our restricted settings to admin users - this is only needed for OctoPrint <= 1.2.16
        restricted = ("token")
        for r in restricted:
            if r in data and (current_user is None or current_user.is_anonymous() or not current_user.is_admin()):
                data[r] = None

        return data

    def get_settings_restricted_paths(self):
        # only used in OctoPrint versions > 1.2.16
        return dict(admin=[["token"]])

    def get_token(self):
        return self._settings.get(["token"])

    def get_settings_defaults(self):
        return dict(
            token=None,
            gotify_server_base_url=None,
            image=True,
            events=dict(
                Scheduled=dict(
                    message=u''.join(
                        [u"Scheduled Notification: {elapsed_time} Minutes Elapsed", self.get_emoji("clock")]),
                    priority="0",
                    custom=True,
                    mod=0
                ),
                Progress=dict(
                    message="Print Progress: {percentage}%",
                    priority="0",
                    custom=True,
                    mod=0
                ),
                TempReached=dict(
                    name="Temperature Reached",
                    message=u''.join([self.get_emoji("temp"),
                                      u"Temperature Reached! Bed: {bed_temp}/{bed_target} | Extruder: {e1_temp}/{e1_target}"]),
                    priority="0",
                ),
                Shutdown=dict(
                    name="Printer Shutdown",
                    message=u''.join(
                        [u"Bye bye, I am shutting down ", self.get_emoji("waving_hand_sign")]),
                    priority="0",
                ),
                Startup=dict(
                    name="Printer Startup",
                    message=u''.join(
                        [u"Hello, Let's print something nice today ", self.get_emoji("waving_hand_sign")]),
                ),
                PrintStarted=dict(
                    name="Print Started",
                    message="Print Job Started",
                    priority="0",
                ),
                PrintDone=dict(
                    name="Print Done",
                    message="Print Job Finished: {file}, Finished Printing in {elapsed_time}",
                    priority="0"
                ),
                PrintFailed=dict(
                    name="Print Failed",
                    message="Print Job Failed: {file}",
                    priority=0
                ),
                PrintPaused=dict(
                    name="Print Paused",
                    help="Send a notification when a Pause event is received. When a <code>m70</code> was sent "
                    "to the printer, the message will be appended to the notification.",
                    message="Print Job Paused {m70_cmd}",
                    priority=0
                ),
                Waiting=dict(
                    name="Printer is Waiting",
                    help="Send a notification when a Waiting event is received. When a <code>m70</code> was sent "
                    "to the printer, the message will be appended to the notification.",
                    message="Printer is Waiting {m70_cmd}",
                    priority=0
                ),
                FilamentChange=dict(
                    name="Filament Change",
                    help="Send a notification when a M600 (Filament Change) command is received. When a <code>m70</code> was sent "
                    "to the printer, the message will be appended to the notification.",
                    message="Please change the filament {m70_cmd}",
                    priority=0
                ),
                ZChange=dict(
                    name="After first couple of layer",
                    help="Send a notification when the 'first' couple of layers is done.",
                    message=u''.join(
                        [u"First couple of layers are done ", self.get_emoji("four_leaf_clover")]),
                    priority=0,
                ),
                Alert=dict(
                    name="Alert Event (M300)",
                    message="Alert! The printer issued a alert (beep) via M300",
                    priority=1,
                    hidden=True
                ),
                EStop=dict(
                    name="Panic Event (M112)",
                    message="Panic!! The printer issued a panic stop (M112)",
                    priority=1,
                    hidden=True
                ),
                # See: src/octoprint/util/comm.py:2009
                Error=dict(
                    name="Error Event",
                    help="This event occurs when for example your temperature sensor disconnects.",
                    message="Error!! An error has occurred in the printer communication. {error}",
                    priority=1,
                    hidden=True
                ),
            )
        )

    def get_template_vars(self):
        return dict(
            events=self.get_settings_defaults()["events"]
        )

    def get_template_configs(self):
        return [
            dict(type="settings", name="Gotify", custom_bindings=True)
        ]

    def get_update_information(self):
        return dict(
            gotify=dict(
                displayName="Gotify Plugin",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="niwla23",
                repo="OctoPrint-Gotify",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/niwla23/OctoPrint-Gotify/archive/{target_version}.zip"
            )
        )


__plugin_name__ = "Gotify"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = GotifyPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.sent_gcode
    }
