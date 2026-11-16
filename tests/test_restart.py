import os
import subprocess
import sys
import sysconfig
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic, sleep

from conftest import Client, running, runtime_credentials
from test_restore import isolated_databases
from test_review_regressions import prepare


@contextmanager
def worker(environment):
    names = ("IEW_WORKBENCH_DSN", "IEW_SIMULATOR_DSN", "IEW_JOURNAL_DSN")
    with runtime_credentials([environment[key] for key in names]) as dsns:
        environment = {**environment, **dict(zip(names, dsns)), "PYTHONPATH": sysconfig.get_path("purelib")}
        executable = getattr(sys, "_base_executable", sys.executable)
        with subprocess.Popen([executable, "-m", "iew", "worker"], env=environment,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


def wait_for(condition, timeout=20):
    expires = monotonic() + timeout
    while monotonic() < expires:
        if condition():
            return
        sleep(0.15)
    raise AssertionError("Expected durable worker state did not arrive")


def test_kill_after_destination_commit_then_restart_reconciles(database):
    from iew.api import WorkbenchServer
    from iew.contracts import now
    from iew.delivery import Delivery
    from iew.simulator import Simulator, SimulatorServer
    from iew.store import Store

    with isolated_databases() as paths:
        store, sim = Store(paths[0]), Simulator(paths[1])
        store.initialize()
        sim.initialize()
        for name,role in [("analyst","analyst"),("peer","approver")]:
            store.add_user(name,"cedar",[role],"test password only")
        sim.original("cedar", "cedar-v1", "customer", "C100", "original-a", now()-timedelta(days=1), "fenced_no_write")
        sim.fault("cedar", "cedar-v1", "customer", "C100", "response_delay_after_commit")
        token = "isolated-test-service-token"
        with running(SimulatorServer(("127.0.0.1", 0), sim, token)) as dest:
            engine = Delivery(store, paths[2], dest, token)
            engine.initialize()
            env = {**os.environ, "IEW_WORKBENCH_DSN": paths[0], "IEW_SIMULATOR_DSN": paths[1],
                   "IEW_JOURNAL_DSN": paths[2], "IEW_SIMULATOR_URL": dest, "IEW_SERVICE_TOKEN": token}
            with running(WorkbenchServer(("127.0.0.1", 0), store, engine)) as url:
                analyst, peer = Client(url).login("analyst"), Client(url).login("peer")
                _, path, revision = prepare(analyst, peer)
                assert peer.call("POST", path+"/send", revision)[0] == 200
                with worker(env):
                    wait_for(lambda: sim.effect_count() == 1)
                stranded = peer.call("GET", path)[1]
                assert stranded["status"] == "Sending" and stranded["attempts"] == 1
                with worker(env):
                    wait_for(lambda: peer.call("GET", path)[1]["status"] == "Confirmed")
                assert sim.effect_count() == 1
                assert peer.call("GET", path)[1]["attempts"] == 1
