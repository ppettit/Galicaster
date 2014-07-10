# -*- coding:utf-8 -*-
# Galicaster, Multistream Recorder and Player
#
#       galicaster/recorder/service
#
# Copyright (c) 2014, Teltek Video Research <galicaster@teltek.es>
#
# This work is licensed under the Creative Commons Attribution-o
# NonCommercial-ShareAlike 3.0 Unported License. To view a copy of
# this license, visit http://creativecommons.org/licenses/by-nc-sa/3.0/
# or send a letter to Creative Commons, 171 Second Street, Suite 300,
# San Francisco, California, 94105, USA.

"""
TODO:
 - Scheduled recordings
 - profile.execute (see recorderui configure_profile)
 - Delete gst dependency.
 - Add connect:
   * start-record
   * stop-record
   * start-before
 - Add doc
"""

from datetime import datetime
import gst
from galicaster.mediapackage import mediapackage
from galicaster.recorder import Recorder
from galicaster.utils.i18n import _
from galicaster.utils.gstreamer import WeakMethod


class Status(object):
    def __init__(self, name): self.name = name
    def __str__(self): return self.name
    def __repr__(self): return self.name

INIT_STATUS = Status('init')
PREVIEW_STATUS = Status('preview')
RECORDING_STATUS = Status('recording')
PAUSED_STATUS = Status('paused')
ERROR_STATUS = Status('error')


class RecorderService(object):
    def __init__(self, dispatcher, repo, worker, conf, logger, recorderklass=Recorder):
        """
        Initialize the recorder service.

        :param dispacher service.
        :param repo service.
        :param worker service.
        :param conf service.
        :param logger service.
        :param recorderklass (only to test) 
        """        
        self.repo = repo
        self.dispatcher = dispatcher
        self.worker = worker
        self.logger = logger
        self.conf = conf
        self.overlap = conf.get_permission("overlap")
        
        self.status = INIT_STATUS

        self.current_mediapackage = None
        self.recorder = None
        self.__recorderklass = recorderklass
        self.__create_drawing_areas_func = None
        self.__handle_recover_id = None

        self.dispatcher.connect("galicaster-init", WeakMethod(self, '_handle_init'))
        self.dispatcher.connect("reload-profile", WeakMethod(self, '_handle_reload_profile'))
        self.dispatcher.connect("recorder-error", WeakMethod(self, '_handle_error'))
 


    def set_create_drawing_areas_func(self, func):
        self.__create_drawing_areas_func = func


    def preview(self):
        if self.status not in (INIT_STATUS, ERROR_STATUS):
            return False
        
        self.logger.info("Starting recording service in the preview status")
        self.__prepare()
        self.recorder.preview()
        self.status = PREVIEW_STATUS
        return True


    def __prepare(self):
        current_profile = self.conf.get_current_profile()
        self.logger.debug("Using {} profile".format(current_profile.name))
        bins = current_profile.tracks
        for objectbin in bins:
            objectbin['path'] = self.repo.get_rectemp_path()

        self.recorder = self.__recorderklass(bins)
        if self.__create_drawing_areas_func:
            info = self.recorder.get_display_areas_info()
            #TODO
            #if self.swap:
            #    info.reverse()
            areas = self.__create_drawing_areas_func(info)
            self.recorder.set_drawing_areas(areas)


    def record(self):
        self.logger.info("Recording")
        if self.status in (INIT_STATUS, ERROR_STATUS):
            self.logger.warning("Cancel recording: status error (in {})".format(self.status))
            return False
        if self.status != PREVIEW_STATUS and not self.overlap:
            self.logger.info("Cancel recording: it is already recording and not allow overlap")
            return False

        if self.status == PAUSED_STATUS:
            self.resume()

        if self.status == RECORDING_STATUS:
            self.recorder.stop()
            self.__close_mp()
            self.__prepare()
            self.recorder.preview_and_record()
        else:
            self.recorder and self.recorder.record()            
        self.current_mediapackage = self.__new_mediapackage(to_record=True)
        self.status = RECORDING_STATUS
        return True


    def stop(self, force=False):
        self.logger.info("Stopping the capture")
        if self.status == PAUSED_STATUS:
            self.resume()
        if self.status != RECORDING_STATUS:
            self.logger.warning("Cancel stop: status error (is {})".format(self.status))
            return False

        self.recorder.stop(force)
        self.__close_mp()

        self.status = INIT_STATUS
        self.preview()
        return True


    def __close_mp(self):
        close_duration = self.recorder.get_recorded_time() / gst.MSECOND
        self.current_mediapackage.status = mediapackage.RECORDED
        self.logger.info("Adding new mediapackage ({}) to the repository".format(
                self.current_mediapackage.getIdentifier()))
        self.repo.add_after_rec(self.current_mediapackage, self.recorder.get_bins_info(),
                                close_duration, self.current_mediapackage.manual)

        code = 'manual' if self.current_mediapackage.manual else 'scheduled'
        if self.conf.get_lower('ingest', code) == 'immediately':
            self.worker.ingest(self.current_mediapackage)
        elif self.conf.get_lower('ingest', code) == 'nightly':
            self.worker.ingest_nightly(self.current_mediapackage)


    def pause(self):
        self.logger.info("Pausing recorder")
        if self.status == RECORDING_STATUS:
            self.recorder.pause()
            self.status = PAUSED_STATUS
            return True
        self.logger.warning("Cancel pause: status error (in {})".format(self.status))
        return False


    def resume(self):
        self.logger.info("Resuming recorder")
        if self.status == PAUSED_STATUS:
            self.recorder.resume()
            self.status = RECORDING_STATUS
            return True
        self.logger.warning("Cancel resume: status error (in {})".format(self.status))
        return False


    def mute_preview(self, value):
        self.recorder and self.recorder.mute_preview(value)


    def get_recorded_time(self):
        return self.recorder.get_recorded_time() if self.recorder else 0


    def _handle_error(self, origin, error_msg):
        self.logger.error("Handle error ({})". format(error_msg))
        self.recorder.stop(True)
        self.status = ERROR_STATUS
        if not self.__handle_recover_id:
            self.logger.debug("Connecting recover recorder callback")
            self.__handle_recover_id = self.dispatcher.connect("galicaster-notify-timer-long", 
                                                             WeakMethod(self, '_handle_recover'))


    def _handle_recover(self, origin):
        self.logger.info("Handle recover from error")
        if self.__handle_recover_id and self.preview(): 
            self.logger.debug("Disconnecting recover recorder callback")
            self.__handle_recover_id = self.dispatcher.disconnect(self.__handle_recover_id)        


    def _handle_init(self, origin):
        self.logger.debug("Init recorder service")
        self.preview()


    def _handle_reload_profile(self, origin):
        if self.status == PREVIEW_STATUS:
            self.logger.debug("Resetting recorder after reloading the profile")
            self.recorder.stop(True)
            self.status = INIT_STATUS
            self.preview()
            

    def __new_mediapackage(self, to_record=False):
        now = datetime.now().replace(microsecond=0)
        title = _("Recording started at {0}").format(now.isoformat())
        mp = mediapackage.Mediapackage(title=title)
        mp.properties['origin'] = self.conf.hostname
        if to_record:
            mp.status = mediapackage.RECORDING
            now = datetime.utcnow().replace(microsecond=0)
            mp.setDate(now)
        return mp


    def __del__(self):
        self.recorder and self.recorder.stop(True)
