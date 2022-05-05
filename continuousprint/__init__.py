# coding=utf-8
from __future__ import absolute_import

import flask
import json
import yaml
import os
import time
from io import BytesIO

import octoprint.plugin
import octoprint.util
from octoprint.server.util.flask import restricted_access
from octoprint.events import Events
from octoprint.access.permissions import Permissions, ADMIN_GROUP
import octoprint.filemanager
from octoprint.filemanager.util import StreamWrapper
from octoprint.filemanager.destinations import FileDestinations
from octoprint.util import RepeatedTimer
from octoprint.printer import InvalidFileLocation, InvalidFileType
from peerprint.server import Server as PeerPrintServer
from peerprint.adapter import Adapter as PeerPrintAdapter

from .driver import Driver, Action as DA, Printer as DP
from .supervisor import Supervisor
from .storage.database import init as init_db
from .storage import queries

QUEUE_KEY = "cp_queue"
DEFAULT_QUEUE = "default"
PRINTER_PROFILE_KEY = "cp_printer_profile"
CLEARING_SCRIPT_KEY = "cp_bed_clearing_script"
FINISHED_SCRIPT_KEY = "cp_queue_finished_script"
TEMP_FILES = dict(
    [(k, f"{k}.gcode") for k in [FINISHED_SCRIPT_KEY, CLEARING_SCRIPT_KEY]]
)
RESTART_MAX_RETRIES_KEY = "cp_restart_on_pause_max_restarts"
RESTART_ON_PAUSE_KEY = "cp_restart_on_pause_enabled"
RESTART_MAX_TIME_KEY = "cp_restart_on_pause_max_seconds"
BED_COOLDOWN_ENABLED_KEY = "bed_cooldown_enabled"
BED_COOLDOWN_SCRIPT_KEY = "cp_bed_cooldown_script"
BED_COOLDOWN_THRESHOLD_KEY = "bed_cooldown_threshold"
BED_COOLDOWN_TIMEOUT_KEY = "bed_cooldown_timeout"
MATERIAL_SELECTION_KEY = "cp_material_selection_enabled"


class OctoPrintAdapter(PeerPrintAdapter):
    def __init__(self, namespace, addr):
        self.ns = namespace
        self.addr = addr
        self.server = server

    def get_namespace(self):
        return self.ns

    def get_host_addr(self):
        return self.addr

    def upsert_job(self, data):
        job = queries.importJob(self.ns, data)
        return job.id

    def remove_job(self, local_id):
        queries.removeJobsAndSets(job_ids=[local_id], set_ids=[])


class ContinuousprintPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def _msg(self, msg="", type="popup"):
        self._plugin_manager.send_plugin_message(
            self._identifier, dict(type=type, msg=msg)
        )

    def _update_driver_settings(self):
        self.d.set_retry_on_pause(
            self._settings.get([RESTART_ON_PAUSE_KEY]),
            self._settings.get([RESTART_MAX_RETRIES_KEY]),
            self._settings.get([RESTART_MAX_TIME_KEY]),
        )

    # part of SettingsPlugin
    def get_settings_defaults(self):
        base = os.path.dirname(__file__)
        with open(os.path.join(base, "data/printer_profiles.yaml"), "r") as f:
            self._printer_profiles = yaml.safe_load(f.read())["PrinterProfile"]
        with open(os.path.join(base, "data/gcode_scripts.yaml"), "r") as f:
            self._gcode_scripts = yaml.safe_load(f.read())["GScript"]

        d = {}
        d[QUEUE_KEY] = "[]"
        d[CLEARING_SCRIPT_KEY] = ""
        d[FINISHED_SCRIPT_KEY] = ""

        for s in self._gcode_scripts:
            name = s["name"]
            gcode = s["gcode"]
            if name == "Pause":
                d[CLEARING_SCRIPT_KEY] = gcode
            elif name == "Generic Off":
                d[FINISHED_SCRIPT_KEY] = gcode
        d[RESTART_MAX_RETRIES_KEY] = 3
        d[RESTART_ON_PAUSE_KEY] = False
        d[RESTART_MAX_TIME_KEY] = 60 * 60
        d[BED_COOLDOWN_ENABLED_KEY] = False
        d[BED_COOLDOWN_SCRIPT_KEY] = "; Put script to run before bed cools here\n"
        d[BED_COOLDOWN_THRESHOLD_KEY] = 30
        d[BED_COOLDOWN_TIMEOUT_KEY] = 60
        d[MATERIAL_SELECTION_KEY] = False
        d[PRINTER_PROFILE_KEY] = "Generic"
        return d

    def _active(self):
        return self.d.state != self.d._state_inactive if hasattr(self, "d") else False

    def _rm_temp_files(self):
        # Clean up any file references from prior runs
        for path in TEMP_FILES.values():
            if self._file_manager.file_exists(FileDestinations.LOCAL, path):
                self._file_manager.remove_file(FileDestinations.LOCAL, path)

    # part of StartupPlugin
    def on_after_startup(self):
        # Turn on "restart on pause" when TSD plugin is detected (must be version 1.8.11 or higher for custom event hook)
        if (
            getattr(
                octoprint.events.Events, "PLUGIN_THESPAGHETTIDETECTIVE_COMMAND", None
            )
            is not None
        ):
            self._logger.info(
                "Has TSD plugin with custom events integration - enabling failure automation"
            )
            self._settings.set([RESTART_ON_PAUSE_KEY], True)
        else:
            self._settings.set([RESTART_ON_PAUSE_KEY], False)

        # SpoolManager plugin isn't required, but does enable material-based printing if it exists
        # Code based loosely on https://github.com/OllisGit/OctoPrint-PrintJobHistory/ (see _getPluginInformation)
        smplugin = self._plugin_manager.plugins.get("SpoolManager")
        if smplugin is not None and smplugin.enabled:
            self._spool_manager = smplugin.implementation
            self._logger.info("SpoolManager found - enabling material selection")
            self._settings.set([MATERIAL_SELECTION_KEY], True)
        else:
            self._spool_manager = None
            self._settings.set([MATERIAL_SELECTION_KEY], False)

        self._settings.save()

        storage_init_path = os.path.join(
            os.path.dirname(__file__), "data/storage_init.yaml"
        )
        plugin_data_dir = self.get_plugin_data_folder()
        init_db(
            db_path=os.path.join(plugin_data_dir, "queue.sqlite3"),
            initial_data_path=storage_init_path,
        )

        profname = self._settings.get([PRINTER_PROFILE_KEY])
        for prof in self._printer_profiles:
            if prof["name"] == profname:
                self._printer_profile = dict(
                    model=prof["model"],
                    width=prof["width"],
                    depth=prof["depth"],
                    height=prof["height"],
                    formFactor=prof["formFactor"],
                    selfClearing=prof["selfClearing"],
                )
                break

        self.peer_server = PeerPrintServer(
            os.path.join(plugin_data_dir, "peerprint.sqlite3"), self._logger
        )
        port = 9876  # TODO better port handling, dynamic queue creation
        for q in queries.getQueues():
            if q.addr is not None:
                self.peer_server.join(PeerPrintAdapter(q.name, q.addr))
                port += 1

        self.s = Supervisor(queries, DEFAULT_QUEUE)
        self.d = Driver(
            supervisor=self.s,
            script_runner=self,
            logger=self._logger,
        )
        self.update(DA.DEACTIVATE)  # Initializes and passes printer state
        self._update_driver_settings()
        self._rm_temp_files()
        self.next_pause_is_spaghetti = False

        # It's possible to miss events or for some weirdness to occur in conditionals. Adding a watchdog
        # timer with a periodic tick ensures that the driver knows what the state of the printer is.
        self.watchdog = RepeatedTimer(5.0, lambda: self.update(DA.TICK))
        self.watchdog.start()
        self._logger.info("Continuous Print Plugin started")

    def _on_db_change(
        self,
        upserted_jobs: list = [],
        upserted_queues: list = [],
        removed_sets: list = [],
        removed_jobs: list = [],
        removed_queues=[],
    ):
        self._logger.info(
            "TODO _on_db_change", upsert, removed_sets, removed_jobs, removed_queues
        )

    def update(self, a: DA):
        # Access current file via `get_current_job` instead of `is_current_file` because the latter may go away soon
        # See https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.PrinterInterface.is_current_file
        # Avoid using payload.get('path') as some events may not express path info.
        path = self._printer.get_current_job().get("file", {}).get("name")
        pstate = self._printer.get_state_id()
        p = DP.BUSY
        if pstate == "OPERATIONAL":
            p = DP.IDLE
        elif pstate == "PAUSED":
            p = DP.PAUSED

        materials = []
        if self._spool_manager is not None:
            # We need *all* selected spools for all tools, so we must look it up from the plugin itself
            # (event payload also excludes color hex string which is needed for our identifiers)
            materials = self._spool_manager.api_getSelectedSpoolInformations()
            materials = [
                f"{m['material']}_{m['colorName']}_{m['color']}"
                if m is not None
                else None
                for m in materials
            ]

        if self.d.action(a, p, path, materials):
            self._msg(type="reload")  # Reload UI when new state is added

        # Send our updates to PeerPrint (if configured)
        if self.peer_server is not None:
            # elapsed = self.s.elapsed()
            # estTime = 1000  # TODO
            # self.peer_server.updatePeer(PeerData(status=pstate, secondsUntilIdle=(estTime-elapsed)))
            pass

    # part of EventHandlerPlugin
    def on_event(self, event, payload):
        if not hasattr(self, "d"):  # Ignore any messages arriving before init
            return

        current_file = self._printer.get_current_job().get("file", {}).get("name")
        is_current_path = current_file == self.d.current_path()

        # Try to fetch plugin-specific events, defaulting to None otherwise

        # This custom event is only defined when OctoPrint-TheSpaghettiDetective plugin is installed.
        tsd_command = getattr(
            octoprint.events.Events, "PLUGIN_THESPAGHETTIDETECTIVE_COMMAND", None
        )
        # This event is only defined when OctoPrint-SpoolManager plugin is installed.
        spool_selected = getattr(
            octoprint.events.Events, "PLUGIN__SPOOLMANAGER_SPOOL_SELECTED", None
        )
        spool_deselected = getattr(
            octoprint.events.Events, "PLUGIN__SPOOLMANAGER_SPOOL_DESELECTED", None
        )

        if event == Events.METADATA_ANALYSIS_FINISHED:
            # OctoPrint analysis writes to the printing file - we must remove
            # our temp files AFTER analysis has finished or else we'll get a "file not found" log error.
            # We do so when either we've finished printing or when the temp file is no longer selected
            if self._printer.get_state_id() != "OPERATIONAL":
                for path in TEMP_FILES.values():
                    if self._printer.is_current_file(path, sd=False):
                        return
            self._rm_temp_files()
        elif event == Events.PRINT_DONE:
            self.update(DA.SUCCESS)
        elif event == Events.PRINT_FAILED:
            # Note that cancelled events are already handled directly with Events.PRINT_CANCELLED
            self.update(DA.FAILURE)
        elif event == Events.PRINT_CANCELLED:
            if payload.get("user") is not None:
                self.update(DA.DEACTIVATE)
            else:
                self.update(DA.TICK)
        elif (
            is_current_path
            and tsd_command is not None
            and event == tsd_command
            and payload.get("cmd") == "pause"
            and payload.get("initiator") == "system"
        ):
            self.update(DA.SPAGHETTI)
        elif spool_selected is not None and event == spool_selected:
            self.update(DA.TICK)
        elif spool_deselected is not None and event == spool_deselected:
            self.update(DA.TICK)
        elif is_current_path and event == Events.PRINT_PAUSED:
            self.update(DA.TICK)
        elif is_current_path and event == Events.PRINT_RESUMED:
            self.update(DA.TICK)
        elif (
            event == Events.PRINTER_STATE_CHANGED
            and self._printer.get_state_id() == "OPERATIONAL"
        ):
            self.update(DA.TICK)
        elif event == Events.UPDATED_FILES:
            self._msg(type="updatefiles")
        elif event == Events.SETTINGS_UPDATED:
            self._update_driver_settings()

    def execute_gcode(self, key):
        gcode = self._settings.get([key])
        file_wrapper = StreamWrapper(key, BytesIO(gcode.encode("utf-8")))
        path = TEMP_FILES[key]
        added_file = self._file_manager.add_file(
            octoprint.filemanager.FileDestinations.LOCAL,
            path,
            file_wrapper,
            allow_overwrite=True,
        )
        self._logger.info(f"Wrote file {path}")
        self._printer.select_file(path, sd=False, printAfterSelect=True)
        return added_file

    def run_finish_script(self):
        self._msg("Print Queue Complete", type="complete")
        return self.execute_gcode(FINISHED_SCRIPT_KEY)

    def cancel_print(self):
        self._msg("Print cancelled", type="error")
        self._printer.cancel_print()

    def wait_for_bed_cooldown(self):
        self._logger.info("Running bed cooldown script")
        bed_cooldown_script = self._settings.get(["cp_bed_cooldown_script"]).split("\n")
        self._printer.commands(bed_cooldown_script, force=True)
        self._logger.info("Preparing for Bed Cooldown")
        self._printer.set_temperature("bed", 0)  # turn bed off
        start_time = time.time()

        while (time.time() - start_time) <= (
            60 * float(self._settings.get(["bed_cooldown_timeout"]))
        ):  # timeout converted to seconds
            bed_temp = self._printer.get_current_temperatures()["bed"]["actual"]
            if bed_temp <= float(self._settings.get(["bed_cooldown_threshold"])):
                self._logger.info(
                    f"Cooldown threshold of {self._settings.get(['bed_cooldown_threshold'])} has been met"
                )
                return

        self._logger.info(
            f"Timeout of {self._settings.get(['bed_cooldown_timeout'])} minutes exceeded"
        )
        return

    def clear_bed(self):
        if self._settings.get(["bed_cooldown_enabled"]):
            self.wait_for_bed_cooldown()
        return self.execute_gcode(CLEARING_SCRIPT_KEY)

    def start_print(self, item):
        self._msg(f"Job {item.job.name}: printing {item.path}")
        self._msg(type="reload")
        try:
            self._logger.info(f"Attempting to print {item.path} (sd={item.sd})")
            self._printer.select_file(item.path, item.sd, printAfterSelect=True)
        except InvalidFileLocation as e:
            self._logger.error(e)
            self._msg("File not found: " + item.path, type="error")
        except InvalidFileType as e:
            self._logger.error(e)
            self._msg("File not gcode: " + item.path, type="error")
        return True

    def state_json(self):
        # IMPORTANT: Non-additive changes to this response string must be released in a MAJOR version bump
        # (e.g. 1.4.1 -> 2.0.0).
        orderedJobs = queries.getJobsAndSets(DEFAULT_QUEUE, lexOrder=True)

        active_set = None
        # Only annotate active jobs/sets if we've started a run
        if self._active() and self.s.run is not None:
            assigned = self.s.get_assignment()
            if assigned is not None:
                active_set = assigned.id

        jobs = []
        for j in orderedJobs:
            jd = j.as_dict(json_safe=True)
            jobs.append(jd)

        resp = {
            "active": self._active(),
            "active_set": active_set,
            "status": "Initializing" if not hasattr(self, "d") else self.d.status,
            "jobs": jobs,
        }

        # This may be a bit query-inefficient, but it's simple. Can optimize later if needed.

        return json.dumps(resp)

    # Listen for resume from printer ("M118 //action:queuego") #from @grtrenchman
    def resume_action_handler(self, comm, line, action, *args, **kwargs):
        if not action == "queuego":
            return
        self.update(DA.ACTIVATE)

    # Public API method returning the full state of the plugin in JSON format.
    # See `state_json()` for return values.
    @octoprint.plugin.BlueprintPlugin.route("/state", methods=["GET"])
    @restricted_access
    def state(self):
        return self.state_json()

    # Public method - enables/disables management and returns the current state
    # IMPORTANT: Non-additive changes to this method MUST be done via MAJOR version bump
    # (e.g. 1.4.1 -> 2.0.0)
    @octoprint.plugin.BlueprintPlugin.route("/set_active", methods=["POST"])
    @restricted_access
    def set_active(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_STARTQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        self.update(
            DA.ACTIVATE if flask.request.form["active"] == "true" else DA.DEACTIVATE
        )
        return self.state_json()

    # PRIVATE API method - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/clear", methods=["POST"])
    @restricted_access
    def clear(self):
        i = 0
        keep_failures = flask.request.form["keep_failures"] == "true"
        keep_non_ended = flask.request.form["keep_non_ended"] == "true"
        self._logger.info(
            f"Clearing queue (keep_failures={keep_failures}, keep_non_ended={keep_non_ended})"
        )
        changed = []
        while i < len(self.q):
            v = self.q[i]
            self._logger.info(f"{v.name} -- end_ts {v.end_ts} result {v.result}")
            if v.end_ts is None and keep_non_ended:
                i = i + 1
            elif v.result == "failure" and keep_failures:
                i = i + 1
            else:
                del self.q[i]
                changed.append(i)
        return self.state_json()

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/set/add", methods=["POST"])
    @restricted_access
    def add_set(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_ADDQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        return json.dumps(
            queries.appendSet(
                DEFAULT_QUEUE, flask.request.form["job"], flask.request.form
            )
        )

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/job/add", methods=["POST"])
    @restricted_access
    def add_job(self):
        if not Permissions.PLUGIN_CONTINUOUSPRINT_ADDQUEUE.can():
            return flask.make_response("Insufficient Rights", 403)
            self._logger.info("attempt failed due to insufficient permissions.")
        j = queries.newEmptyJob(DEFAULT_QUEUE)
        return json.dumps(j.as_dict(json_safe=True))

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/set/mv", methods=["POST"])
    @restricted_access
    def mv_set(self):
        self.s.clear_cache()  # API call affects runs; assignment may have changed
        queries.moveSet(
            int(flask.request.form["id"]),
            int(
                flask.request.form["after_id"]
            ),  # Move to after this set (-1 for beginning of job)
            int(
                flask.request.form["dest_job"]
            ),  # Move to this job (null for new job at end)
        )
        return json.dumps("ok")

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/set/update", methods=["POST"])
    @restricted_access
    def update_set(self):
        self.s.clear_cache()  # API call affects runs; assignment may have changed
        return json.dumps(
            queries.updateSet(
                flask.request.form["id"],
                flask.request.form,
                json_safe=True,
            )
        )

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/job/mv", methods=["POST"])
    @restricted_access
    def mv_job(self):
        self.s.clear_cache()  # API call affects runs; assignment may have changed
        queries.moveJob(
            int(flask.request.form["id"]),
            int(
                flask.request.form["after_id"]
            ),  # Move to after this job (-1 for beginning of queue)
        )
        return json.dumps("ok")

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/job/update", methods=["POST"])
    @restricted_access
    def update_job(self):
        self.s.clear_cache()  # API call affects runs; assignment may have changed
        return json.dumps(
            queries.updateJob(
                flask.request.form["id"], flask.request.form, json_safe=True
            )
        )

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/multi/rm", methods=["POST"])
    @restricted_access
    def rm_multi(self):
        self.s.clear_cache()  # API call affects runs; assignment may have changed
        jids = flask.request.form.getlist("job_ids[]")
        sids = flask.request.form.getlist("set_ids[]")
        qids = flask.request.form.getlist("queue_ids[]")

        result = queries.removeJobsAndSets(jids, sids)
        if qids is not None:
            result["queues_deleted"] = queries.removeQueues(qids)
        return json.dumps(result)

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/multi/reset", methods=["POST"])
    @restricted_access
    def reset_multi(self):
        self.s.clear_cache()  # API call affects runs; assignment may have changed
        jids = flask.request.form.getlist("job_ids[]")
        sids = flask.request.form.getlist("set_ids[]")
        return json.dumps(queries.replenish(jids, sids))

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/history", methods=["GET"])
    @restricted_access
    def get_history(self):
        h = queries.getHistory()
        if self.s.run is not None:
            for row in h:
                if row["run_id"] == self.s.run:
                    row["active"] = True
                    break
        return json.dumps(h)

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/clearHistory", methods=["POST"])
    @restricted_access
    def clear_history(self):
        queries.clearHistory()
        return json.dumps("OK")

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/queues", methods=["GET"])
    @restricted_access
    def get_queues(self):
        return json.dumps([q.as_dict() for q in queries.getQueues()])

    # PRIVATE API METHOD - may change without warning.
    @octoprint.plugin.BlueprintPlugin.route("/queues/commit", methods=["POST"])
    @restricted_access
    def commit_queues(self):
        self._logger.info("/queues/commit")
        for qdata in json.loads(flask.request.form.get("queues")):
            self._logger.info(qdata)
            queries.upsertQueue(qdata["name"], qdata["strategy"], qdata["addr"])
        # TODO remove non-present queues
        return json.dumps("OK")

    # part of TemplatePlugin
    def get_template_vars(self):
        return dict(
            printer_profiles=self._printer_profiles, gcode_scripts=self._gcode_scripts
        )

    def get_template_configs(self):
        return [
            dict(
                type="settings",
                custom_bindings=True,
                template="continuousprint_settings.jinja2",
            ),
            dict(
                type="tab",
                name="Continuous Print",
                custom_bindings=False,
                template="continuousprint_tab.jinja2",
            ),
            dict(
                type="tab",
                name="CPQ History",
                custom_bindings=False,
                template="continuousprint_history_tab.jinja2",
            ),
        ]

    # part of AssetPlugin
    def get_assets(self):
        return dict(
            js=[
                "js/cp_modified_sortable.js",
                "js/cp_modified_knockout-sortable.js",
                "js/continuousprint_api.js",
                "js/continuousprint_history_row.js",
                "js/continuousprint_history.js",
                "js/continuousprint_set.js",
                "js/continuousprint_job.js",
                "js/continuousprint_viewmodel.js",
                "js/continuousprint_settings.js",
                "js/continuousprint.js",
            ],
            css=["css/continuousprint.css"],
        )

    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return dict(
            continuousprint=dict(
                displayName="Continuous Print Plugin",
                displayVersion=self._plugin_version,
                # version check: github repository
                type="github_release",
                user="smartin015",
                repo="continuousprint",
                current=self._plugin_version,
                stable_branch=dict(
                    name="Stable", branch="master", comittish=["master"]
                ),
                prerelease_branches=[
                    dict(
                        name="Release Candidate",
                        branch="rc",
                        comittish=["rc", "master"],
                    )
                ],
                # update method: pip
                pip="https://github.com/smartin015/continuousprint/archive/{target_version}.zip",
            )
        )

    def add_permissions(*args, **kwargs):
        return [
            dict(
                key="STARTQUEUE",
                name="Start Queue",
                description="Allows for starting and stopping queue",
                roles=["admin", "continuousprint-start"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
            dict(
                key="ADDQUEUE",
                name="Add to Queue",
                description="Allows for adding prints to the queue",
                roles=["admin", "continuousprint-add"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
            dict(
                key="RMQUEUE",
                name="Remove Print from Queue ",
                description="Allows for removing prints from the queue",
                roles=["admin", "continuousprint-remove"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
            dict(
                key="CHQUEUE",
                name="Move items in Queue ",
                description="Allows for moving items in the queue",
                roles=["admin", "continuousprint-move"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
        ]


__plugin_name__ = "Continuous Print"
__plugin_pythoncompat__ = ">=3.6,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = ContinuousprintPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.access.permissions": __plugin_implementation__.add_permissions,
        "octoprint.comm.protocol.action": __plugin_implementation__.resume_action_handler,
        # register to listen for "M118 //action:" commands
    }
