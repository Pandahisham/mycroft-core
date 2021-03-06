# Copyright 2016 Mycroft AI, Inc.
#
# This file is part of Mycroft Core.
#
# Mycroft Core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mycroft Core is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mycroft Core.  If not, see <http://www.gnu.org/licenses/>.

import subprocess
import sys
import threading
import time
from Queue import Queue
from alsaaudio import Mixer
from threading import Thread

import os
import serial

from mycroft.client.enclosure.arduino import EnclosureArduino
from mycroft.client.enclosure.eyes import EnclosureEyes
from mycroft.client.enclosure.mouth import EnclosureMouth
from mycroft.client.enclosure.weather import EnclosureWeather
from mycroft.configuration import ConfigurationManager
from mycroft.messagebus.client.ws import WebsocketClient
from mycroft.messagebus.message import Message
from mycroft.util import play_wav
from mycroft.util import create_signal
from mycroft.util import str2bool
from mycroft.util.audio_test import record
from mycroft.util.log import getLogger

__author__ = 'aatchison + jdorleans + iward + penrods'

LOGGER = getLogger("EnclosureClient")


class EnclosureReader(Thread):
    """
    Reads data from Serial port.

    Listens to all commands sent by Arduino that must be be performed on
    Mycroft Core.

    E.g. Mycroft Stop Feature
        #. Arduino sends a Stop command after a button press on a Mycroft unit
        #. ``EnclosureReader`` captures the Stop command
        #. Notify all Mycroft Core processes (e.g. skills) to be stopped

    Note: A command is identified by a line break
    """

    def __init__(self, serial, client):
        super(EnclosureReader, self).__init__(target=self.read)
        self.alive = True
        self.daemon = True
        self.serial = serial
        self.client = client
        self.start()

    def read(self):
        while self.alive:
            try:
                data = self.serial.readline()[:-2]
                if data:
                    self.process(data)
                    LOGGER.info("Reading: " + data)
            except Exception as e:
                LOGGER.error("Reading error: {0}".format(e))

    def process(self, data):
        self.client.emit(Message(data))

        if "mycroft.stop" in data:
            create_signal('buttonPress')
            self.client.emit(Message("mycroft.stop"))

        if "volume.up" in data:
            self.client.emit(
                Message("IncreaseVolumeIntent", metadata={'play_sound': True}))

        if "volume.down" in data:
            self.client.emit(
                Message("DecreaseVolumeIntent", metadata={'play_sound': True}))

        if "system.test.begin" in data:
            self.client.emit(Message('recognizer_loop:sleep'))

        if "system.test.end" in data:
            self.client.emit(Message('recognizer_loop:wake_up'))

        if "mic.test" in data:
            mixer = Mixer()
            prev_vol = mixer.getvolume()[0]
            mixer.setvolume(35)
            self.client.emit(Message("speak", metadata={
                'utterance': "I am testing one two three"}))

            time.sleep(0.5)  # Prevents recording the loud button press
            record("/tmp/test.wav", 3.0)
            mixer.setvolume(prev_vol)
            play_wav("/tmp/test.wav")
            time.sleep(3.5)  # Pause between tests so it's not so fast

            # Test audio muting on arduino
            subprocess.call('speaker-test -P 10 -l 0 -s 1', shell=True)

        if "unit.shutdown" in data:
            self.client.emit(
                Message("enclosure.eyes.timedspin",
                        metadata={'length': 12000}))
            self.client.emit(Message("enclosure.mouth.reset"))
            subprocess.call('systemctl poweroff -i', shell=True)

        if "unit.reboot" in data:
            self.client.emit(
                Message("enclosure.eyes.spin"))
            self.client.emit(Message("enclosure.mouth.reset"))
            subprocess.call('systemctl reboot -i', shell=True)

        if "unit.setwifi" in data:
            self.client.emit(Message("mycroft.wifi.start"))

        if "unit.factory-reset" in data:
            subprocess.call(
                'rm ~/.mycroft/identity/identity.json',
                shell=True)
            self.client.emit(
                Message("enclosure.eyes.spin"))
            self.client.emit(Message("enclosure.mouth.reset"))
            subprocess.call('systemctl reboot -i', shell=True)

    def stop(self):
        self.alive = False
        self.join()


class EnclosureWriter(Thread):
    """
    Writes data to Serial port.
        #. Enqueues all commands received from Mycroft enclosures
           implementation
        #. Process them on the received order by writing on the Serial port

    E.g. Displaying a text on Mycroft's Mouth
        #. ``EnclosureMouth`` sends a text command
        #. ``EnclosureWriter`` captures and enqueue the command
        #. ``EnclosureWriter`` removes the next command from the queue
        #. ``EnclosureWriter`` writes the command to Serial port

    Note: A command has to end with a line break
    """

    def __init__(self, serial, client, size=16):
        super(EnclosureWriter, self).__init__(target=self.flush)
        self.alive = True
        self.daemon = True
        self.serial = serial
        self.client = client
        self.commands = Queue(size)
        self.start()

    def flush(self):
        while self.alive:
            try:
                cmd = self.commands.get()
                self.serial.write(cmd + '\n')
                LOGGER.info("Writing: " + cmd)
                self.commands.task_done()
            except Exception as e:
                LOGGER.error("Writing error: {0}".format(e))

    def write(self, command):
        self.commands.put(str(command))

    def stop(self):
        self.alive = False
        self.join()


class Enclosure:
    """
    Serves as a communication interface between Arduino and Mycroft Core.

    This interface is currently designed to shut itself down when run on
    a Raspberry Pi that is not connected to a Mycroft Mark 1 board on
    the serial port.  So it can safely be started on any Raspberry Pi.

    ``Enclosure`` initializes and aggregates all enclosures implementation.

    E.g. ``EnclosureEyes``, ``EnclosureMouth`` and ``EnclosureArduino``

    It also listens to the basis events in order to perform those core actions
    on the unit.

    E.g. Start and Stop talk animation
    """

    def __init__(self):
        resetSelf = False
        try:
            self.config = ConfigurationManager.get().get("enclosure")

            platform = self.config.get('platform')
            if platform is None:
                # Use the serial port to check if this is a Mycroft
                # Mark 1 unit.
                platform = self.detect_platform()
                if platform == 'unknown':
                    # Since this is a semi-permanent detection, be
                    # certain!  Sometimes noise on the serial line
                    # causes a faulty mis-detection on a real
                    # Mycroft Mark 1
                    platform = self.detect_platform()

                ConfigurationManager.set('enclosure', 'platform', platform)

                # After a platform detection, the system is usually
                # already active, so the message from the loop
                # has already gone by on the messagebus.  So self reset.
                resetSelf = True
        except Exception as e:
            self.disconnect()
            raise Exception("Exception: Unable to determine platform\n" +
                            str(e))

        LOGGER.info("Platform = '" + platform + "'")
        if platform == "mycroft_mark_1":
            # We are a mycroft_mark_1 unit, start up the
            # enclosure client to communicate over the
            # serial port
            self.__init_serial()
        else:
            self.disconnect()
            raise Exception("Exception: Not a Mycroft Mark 1, shutting down")

        self.client = WebsocketClient()
        self.reader = EnclosureReader(self.serial, self.client)
        self.writer = EnclosureWriter(self.serial, self.client)

        # Create helpers to handle the various parts of the enclosure
        self.eyes = EnclosureEyes(self.client, self.writer)
        self.mouth = EnclosureMouth(self.client, self.writer)
        self.system = EnclosureArduino(self.client, self.writer)

        # TODO: Remove EnclosureWeather once the Skill can send images
        #       directly to the EnclosureAPI.
        self.weather = EnclosureWeather(self.client, self.writer)
        self.__register_events()

        if resetSelf:
            self.__handle_reset(None)

    def detect_platform(self):
        LOGGER.info("Auto-detecting platform")
        try:
            self.__init_serial()

            # Thoroughly flush the serial port.  Since this happens
            # right after boot, sometimes there is junk on the serial
            # port line from electrical noise on a Pi.
            time.sleep(3)
            self.serial.flushInput()
            self.serial.flushOutput()

            # Write "system.ping"
            self.serial.write("system.ping")
            time.sleep(1)

            # Now check to see if we got a response from the ping command
            # command we just sent.  Remember, there might not be a Mark 1
            # Arduino on the other side of the serial port.
            data = self.serial.readline()
            LOGGER.info("Serial response: '" + data + "'")

            # A Mycroft Arduino echos the output to the serial line
            # prefixed by "Command: ".  This assumes only a Mycroft
            # Arduino responds to this, which is probably a dubious
            # assumption, but a decent enough auto-detect for now.
            if "Command: system.ping" in data:
                # We have a Mycroft Mark 1!

                # The Arduino returns a version number in the response
                # with recent builds, but not with older builds.
                # Empty the serial queue of all responses to the ping.
                time.sleep(0.5)
                self.serial.flushInput()

                return "mycroft_mark_1"
        except:
            pass

        # Likely running on a generic Raspberry Pi or Linux desktop
        return "unknown"

    def setup(self):
        must_upload = self.config.get('must_upload')
        if must_upload is not None and str2bool(must_upload):
            ConfigurationManager.set('enclosure', 'must_upload', False)
            time.sleep(5)
            self.client.emit(Message("speak", metadata={
                'utterance': "I am currently uploading to the arduino."}))
            self.client.emit(Message("speak", metadata={
                'utterance': "I will be finished in just a moment."}))
            self.upload_hex()
            self.client.emit(Message("speak", metadata={
                'utterance': "Arduino programing complete."}))

        must_start_test = self.config.get('must_start_test')
        if must_start_test is not None and str2bool(must_start_test):
            ConfigurationManager.set('enclosure', 'must_start_test', False)
            time.sleep(0.5)  # Ensure arduino has booted
            self.client.emit(Message("speak", metadata={
                'utterance': "Begining hardware self test."}))
            self.writer.write("test.begin")

    @staticmethod
    def upload_hex():
        old_path = os.getcwd()
        try:
            os.chdir('/opt/enclosure/')
            subprocess.check_call('./upload.sh')
        finally:
            os.chdir(old_path)

    def __init_serial(self):
        if getattr(self, 'serial', None) is not None:
            return  # already initialized

        LOGGER.info("Opening serial port")
        try:
            self.port = self.config.get("port")
            self.rate = int(self.config.get("rate"))
            self.timeout = int(self.config.get("timeout"))
            self.serial = serial.serial_for_url(
                url=self.port, baudrate=self.rate, timeout=self.timeout)
            LOGGER.info(
                "Connected to: " + self.port + " rate: " + str(self.rate) +
                " timeout: " + str(self.timeout))
        except:
            LOGGER.error(
                "It is not possible to connect to serial port: " + self.port)
            raise

    def __register_events(self):
        self.client.on('mycroft.paired', self.__update_events)
        self.client.on('enclosure.reset', self.__handle_reset)
        self.client.on('enclosure.mouth.listeners', self.__mouth_listeners)
        self.__register_mouth_events()

    def __mouth_listeners(self, event=None):
        if event and event.metadata:
            active = event.metadata['active']
            if active:
                self.__register_mouth_events()
            else:
                self.__remove_mouth_events()

    def __register_mouth_events(self):
        self.client.on('recognizer_loop:record_begin', self.mouth.listen)
        self.client.on('recognizer_loop:record_end', self.mouth.reset)
        self.client.on('recognizer_loop:audio_output_start', self.mouth.talk)
        self.client.on('recognizer_loop:audio_output_end', self.mouth.reset)

    def __remove_mouth_events(self):
        self.client.remove('recognizer_loop:record_begin', self.mouth.listen)
        self.client.remove('recognizer_loop:record_end', self.mouth.reset)
        self.client.remove('recognizer_loop:audio_output_start',
                           self.mouth.talk)
        self.client.remove('recognizer_loop:audio_output_end',
                           self.mouth.reset)

    def __handle_reset(self, event=None):
        # Reset both the mouth and the eye elements to indicate the unit is
        # ready for input.
        self.writer.write("eyes.reset")
        self.writer.write("mouth.reset")

    def __update_events(self, event=None):
        if event and event.metadata:
            if event.metadata.get('paired', False):
                self.__register_mouth_events()
            else:
                self.__remove_mouth_events()

    def run(self):
        try:
            self.client.run_forever()
        except Exception as e:
            LOGGER.error("Client error: {0}".format(e))
            self.stop()

    def disconnect(self):
        if getattr(self, 'serial', None) is not None:
            self.serial.close()
            self.serial = None

    def stop(self):
        self.writer.stop()
        self.reader.stop()
        self.disconnect()


def main():
    try:
        enclosure = Enclosure()
        t = threading.Thread(target=enclosure.run)
        t.start()
        enclosure.setup()
        t.join()
    except Exception as e:
        print(e)
    finally:
        sys.exit()


if __name__ == "__main__":
    main()
