#
# PCP integration
#
# Author: BOFH (bo@suse.de)
#


import multiprocessing
import uuid
import os
import time
import tempfile

from infaketure import cli_msg
from infaketure import ERROR
from infaketure.sshcall import SSHCall


class PCPConnector(object):
    """
    PCP connector class which records/snapshots the values and extracts the data.
    """
    CFG_HOST = "__pcp_host_"
    CFG_LOGGER_HOST = "__pcp_logger_host"
    CFG_PATH = "__pcp_path_to_snapshots_"
    CFG_PROBES = "__pcp_probes_"
    CFG_USER = "__pcp_user_"
    CFG_INTERVAL = "__pcp_interval"

    def __init__(self, config):
        """
        Constructor
        :return:
        """
        self._id = uuid.uuid4().hex
        self._host = config.get(self.CFG_HOST)
        self._logger_host = config.get(self.CFG_LOGGER_HOST, self._host)
        self._snapshots = config.get(self.CFG_PATH, '/tmp/infaketure')
        self.probes = config.get(self.CFG_PROBES, dict())

        if not self._host:
            raise Exception("PCP needs host specified")
        elif not config.get(self.CFG_USER):
            raise Exception("PCP needs user specified")

        self._interval = int(config.get(self.CFG_INTERVAL, 1))
        self._ssh = SSHCall(config.get(self.CFG_USER), self._logger_host)
        self._dest_root = os.path.join(self._snapshots, self._host, self._id)
        self.__process = None
        self.__folio = os.path.join(self._dest_root, time.strftime("%Y%m%d-%H%M%S", time.localtime()))

    def _get_logger_config(self):
        """
        Create PCP logger configuration.

        :return:
        """
        # NOTE: This is a pretty simple version,
        #       which creates only default scope.

        out = list()
        out.append("#pmlogger Version 1")
        out.append("")
        out.append("log mandatory on default {")
        for probe in sorted(self.probes.keys()):
            params = self.probes[probe]
            if not params:
                params = ""
            else:
                params = " [ {0} ]".format(" ".join(['"{0}"'.format(elm) for elm in params]))
            out.append("\t{0}{1}".format(probe, params))
        out.append("}")
        out.append("")

        return os.linesep.join(out)

    def _start(self):
        """
        Start pmlogger.

        :return:
        """
        msg_or_path, stat = self._ssh.call("which pmlogger")
        if stat:
            self.cleanup()
            raise Exception('No PCP logger found: ' + msg_or_path)

        cmd = "{pm_logger} -r -c {pm_config} -h {pm_host} -x0 -l {pm_sys_log} -t {pm_interval}.000000 {pm_folio}"
        pm_sys_log = os.path.join(self._dest_root, "pmloader.log")
        msg, stat = self._ssh.call(cmd.format(pm_config=os.path.join(self._dest_root, "pmloader.config"),
                                              pm_sys_log=pm_sys_log,
                                              pm_interval=self._interval, pm_host=self._host, pm_logger=msg_or_path,
                                              pm_folio=self.__folio))
        if stat:
            log_msg, log_stat = self._ssh.call("cat {pm_sys_log}".format(pm_sys_log=pm_sys_log))
            error_msg = "PCP logger quit unexpectedly ({stat}): '{error}'.\nLog:\n{br}\n{log}\n{br}\n".format(
                stat=stat, error=msg, log=log_msg, br=("-" * 80))
            cli_msg(ERROR, error_msg)

    def _prepare(self):
        """
        Prepare start
        :return:
        """
        self._ssh.check_keypair()
        osh, tmp_path = tempfile.mkstemp()
        tmph = open(tmp_path, "w")
        tmph.write(self._get_logger_config())
        tmph.close()

        msg, stat = self._ssh.call("mkdir -p {0}".format(self._dest_root))
        if stat:
            raise Exception('Error making directory "{0}": {1}'.format(self._dest_root, msg))

        self._ssh.copy_to(tmp_path, os.path.join(self._dest_root, "pmloader.config"))
        os.unlink(tmp_path)

    def cleanup(self):
        """
        Cleanup the data.

        :return:
        """
        msg, stat = self._ssh.call("rm -rf {0}".format(self._snapshots))
        if stat:
            raise Exception('Unable to remove "{0}": {1}'.format(self._snapshots, msg))

    def start(self):
        """
        Start recording the load.

        :return: None
        """
        if self.__process is not None:
            return

        self._prepare()

        self.__process = multiprocessing.Process(target=self._start)
        self.__process.daemon = True
        self.__process.start()

    def stop(self):
        """
        Stop recording the load.

        :return: None
        """
        if self.__process is None:
            return
        self.__process.terminate()
        self.__process = None

    def get_metrics(self, probe):
        """
        Get a data for particular probe.

        :param probe:
        :return: Probe data or None if not found
        """
        if self.__folio is None:
            raise Exception("Unknown portfolio")

        msg, stat = self._ssh.call("which pmval")
        if stat:
            raise Exception('PCP value extractor was not found on "{0}".'.format(self._host))

        data, status = self._ssh.call("pmval -a {pm_folio} {pm_probe}".format(pm_folio=self.__folio, pm_probe=probe),
                                      ignore_failure=True)
        # Parse metric
        metric = {"data": list(), "interval_seconds": self._interval}
        for line in data.split(os.linesep):
            meta = [item.strip() for item in line.split(":", 1)]
            if meta[0] in ["metric", "start", "end", "semantics", "units", "samples"]:
                metric.update(dict([tuple(meta)]))
            records = [item.strip() for item in line.replace("\t", " ").split(" ", 1)]
            if len(records[0].replace(".", ":").split(":")) == 4 and records[1].lower().find("no values") < 0:
                metric["data"].append(records[1])

        return metric
