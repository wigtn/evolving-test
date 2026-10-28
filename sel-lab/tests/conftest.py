"""테스트 공통: 실행 상태(runs/)는 임시 디렉터리로, LLM은 끈다. sel_lab을 import하기 전에 설정해야 한다."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SEL_RUNS", tempfile.mkdtemp(prefix="sel-runs-"))
os.environ["SEL_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
