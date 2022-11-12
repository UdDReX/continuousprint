import time
from io import BytesIO

from pathlib import Path
from octoprint.filemanager.util import StreamWrapper
from octoprint.filemanager.destinations import FileDestinations
from octoprint.printer import InvalidFileLocation, InvalidFileType
from octoprint.server import current_user
from .storage.lan import ResolveError
from .data import TEMP_FILE_DIR, CustomEvents
from .storage.queries import genEventScript


class ScriptRunner:
    def __init__(
        self,
        msg,
        file_manager,
        logger,
        printer,
        refresh_ui_state,
        fire_event,
    ):
        self._msg = msg
        self._file_manager = file_manager
        self._logger = logger
        self._printer = printer
        self._refresh_ui_state = refresh_ui_state
        self._fire_event = fire_event

    def _get_user(self):
        return current_user.get_name()

    def _wrap_stream(self, name, gcode):
        return StreamWrapper(name, BytesIO(gcode.encode("utf-8")))

    def _execute_gcode(self, evt, gcode):
        file_wrapper = self._wrap_stream(evt, gcode)
        path = str(Path(TEMP_FILE_DIR) / f"{evt}.gcode")
        added_file = self._file_manager.add_file(
            FileDestinations.LOCAL,
            path,
            file_wrapper,
            allow_overwrite=True,
        )
        self._logger.info(f"Wrote file {path}")
        self._printer.select_file(
            path, sd=False, printAfterSelect=True, user=self._get_user()
        )
        return added_file

    def _do_msg(self, evt):
        if evt == CustomEvents.FINISH:
            self._msg("Print Queue Complete", type="complete")
        elif evt == CustomEvents.CANCEL:
            self._msg("Print cancelled", type="error")
        elif evt == CustomEvetns.BED_COOLDOWN:
            self._msg("Running bed cooldown script")
        elif evt == CustomEvents.CLEARING:
            self._msg("Clearing bed")

    def run_script_for_event(self, evt, msg=None, msgtype=None):
        self._do_msg(evt)
        gcode = genEventScript(evt)

        # Cancellation happens before custom scripts are run
        if evt == CustomEvents.CANCEL:
            self._printer.cancel_print()

        if gcode != "":
            result = self._execute_gcode(evt, gcode)

        # Bed cooldown turn-off happens after custom scripts are run
        if evt == CustomEvents.COOLDOWN:
            self._printer.set_temperature("bed", 0)  # turn bed off

        self._fire_event(evt)
        return result

    def start_print(self, item):
        self._msg(f"{item.job.name}: printing {item.path}")

        path = item.path
        # LAN set objects may not link directly to the path of the print file.
        # In this case, we have to resolve the path by syncing files / extracting
        # gcode files from .gjob. This works without any extra FileManager changes
        # only becaue self._fileshare was configured with a basedir in the OctoPrint
        # file structure
        if hasattr(item, "resolve"):
            try:
                path = item.resolve()
            except ResolveError as e:
                self._logger.error(e)
                self._msg(f"Could not resolve LAN print path for {path}", type="error")
                return False
            self._logger.info(f"Resolved LAN print path to {path}")

        try:
            self._logger.info(f"Attempting to print {path} (sd={item.sd})")
            self._printer.select_file(
                path, sd=item.sd, printAfterSelect=True, user=self._get_user()
            )
            self._fire_event(CustomEvents.START_PRINT)
        except InvalidFileLocation as e:
            self._logger.error(e)
            self._msg("File not found: " + path, type="error")
            return False
        except InvalidFileType as e:
            self._logger.error(e)
            self._msg("File not gcode: " + path, type="error")
            return False
        self._refresh_ui_state()
        return True
