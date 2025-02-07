import time
from io import BytesIO, StringIO

from asteval import Interpreter
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
        self._symbols = dict(
            current=dict(),
            external=dict(),
            metadata=dict(),
        )

    def _get_user(self):
        try:
            return current_user.get_name()
        except AttributeError:
            return None

    def _wrap_stream(self, name, gcode):
        return StreamWrapper(name, BytesIO(gcode.encode("utf-8")))

    def _execute_gcode(self, evt, gcode):
        file_wrapper = self._wrap_stream(evt.event, gcode)
        path = str(Path(TEMP_FILE_DIR) / f"{evt.event}.gcode")
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

    def _do_msg(self, evt, running=False):
        if evt == CustomEvents.FINISH:
            self._msg("Print Queue Complete", type="complete")
        elif evt == CustomEvents.PRINT_CANCEL:
            self._msg("Print cancelled", type="error")

        if running:
            if evt == CustomEvents.COOLDOWN:
                self._msg("Running bed cooldown script")
            elif evt == CustomEvents.PRINT_SUCCESS:
                self._msg("Running success script")
            elif evt == CustomEvents.AWAITING_MATERIAL:
                self._msg("Running script while awaiting material")

    def set_current_symbols(self, symbols):
        last_path = self._symbols["current"].get("path")
        self._symbols["current"] = symbols.copy()

        # Current state can change metadata
        path = self._symbols["current"].get("path")
        if (
            path is not None
            and path != last_path
            and self._file_manager.file_exists(FileDestinations.LOCAL, path)
            and self._file_manager.has_analysis(FileDestinations.LOCAL, path)
        ):
            # See https://docs.octoprint.org/en/master/modules/filemanager.html#octoprint.filemanager.analysis.GcodeAnalysisQueue
            # for analysis values - or `.metadata.json` within .octoprint/uploads
            self._symbols["metadata"] = self._file_manager.get_metadata(
                FileDestinations.LOCAL, path
            )

    def set_external_symbols(self, symbols):
        assert type(symbols) is dict
        self._symbols["external"] = symbols

    def _get_interpreter(self):
        out = StringIO()
        err = StringIO()
        interp = Interpreter(writer=out, err_writer=err)
        # Merge in so default symbols (e.g. exceptions) are retained
        for (k, v) in self._symbols.items():
            interp.symtable[k] = v
        return interp, out, err

    def run_script_for_event(self, evt, msg=None, msgtype=None):
        interp, out, err = self._get_interpreter()
        gcode = genEventScript(evt, interp, self._logger)
        if len(interp.error) > 0:
            for err in interp.error:
                self._logger.error(err.get_error())
                self._msg(
                    f"CPQ {evt.displayName} Preprocessor:\n{err.get_error()}",
                    type="error",
                )
            gcode = "@pause"  # Exceptions mean we must wait for the user to act
        else:
            err.seek(0)
            err_output = err.read().strip()
            if len(err_output) > 0:
                self._logger.error(err_output)
            out.seek(0)
            interp_output = out.read().strip()
            if len(interp_output) > 0:
                self._msg(f"CPQ {evt.displayName} Preprocessor:\n{interp_output}")
            else:
                self._do_msg(evt, running=(gcode != ""))

        # Cancellation happens before custom scripts are run
        if evt == CustomEvents.PRINT_CANCEL:
            self._printer.cancel_print()

        result = self._execute_gcode(evt, gcode) if gcode != "" else None

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
            self._fire_event(CustomEvents.PRINT_START)
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
