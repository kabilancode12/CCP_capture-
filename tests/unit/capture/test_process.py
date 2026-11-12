import sys
import time
from pathlib import Path

from ccp.capture.process import spawn_detached


def test_a_detached_process_runs_on_its_own_and_the_caller_does_not_wait(tmp_path: Path) -> None:
    marker = tmp_path / "done.txt"
    code = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('ran')"

    started = time.monotonic()
    spawn_detached([sys.executable, "-c", code])
    returned_after = time.monotonic() - started

    assert returned_after < 0.9  # the child sleeps for a second: we did not wait for it
    assert not marker.exists()
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.read_text(encoding="utf-8") == "ran"  # but it did run to the end


def test_the_detached_process_gets_no_input_and_its_output_is_discarded(tmp_path: Path) -> None:
    marker = tmp_path / "stdin-state.txt"
    code = (
        "import sys; print('noise'); data = sys.stdin.read(); "
        f"open({str(marker)!r}, 'w').write(repr(data))"
    )
    spawn_detached([sys.executable, "-c", code])
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.read_text(encoding="utf-8") == "''"  # standard input was empty, not inherited
