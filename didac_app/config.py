from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class DidacConfig:
    database_url: str = os.getenv("DATABASE_URL", "")
    export_dir: Path = Path(os.getenv("DIDAC_EXPORT_DIR", "outputs/didac"))
    media_dir: Path = Path(os.getenv("DIDAC_MEDIA_DIR", "data/didac_media"))
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_bucket: str = os.getenv("SUPABASE_BUCKET", "")
    supabase_secret_key: str = os.getenv("SUPABASE_SECRET_KEY", "")
    bird_workspace_id: str = os.getenv("BIRD_WORKSPACE_ID", "")
    bird_channel_id: str = os.getenv("BIRD_CHANNEL_ID", "")
    bird_access_key: str = os.getenv("BIRD_ACCESS_KEY", "")
    bird_webhook_token: str = os.getenv("BIRD_WEBHOOK_TOKEN", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    secret_key: str = os.getenv("SECRET_KEY", "")
    brochure_url: str = os.getenv("BROCHURE_URL", "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf")  # placeholder PDF until the real one is set
    sia_brochure_url: str = os.getenv("SIA_BROCHURE_URL", "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf")  # placeholder PDF until the real one is set
    tiqer_brochure_url: str = os.getenv("TIQER_BROCHURE_URL", "https://www.orimi.com/pdf-test.pdf")  # placeholder PDF until the real one is set
    play_lab_brochure_url: str = os.getenv("PLAY_LAB_BROCHURE_URL", "https://pdfobject.com/pdf/sample.pdf")  # placeholder PDF until the real one is set
    stream_brochure_url: str = os.getenv("STREAM_BROCHURE_URL", "https://www.orimi.com/pdf-test.pdf")  # placeholder PDF until the real one is set
    lab_in_classroom_brochure_url: str = os.getenv("LAB_IN_CLASSROOM_BROCHURE_URL", "https://pdfobject.com/pdf/sample.pdf")  # placeholder PDF until the real one is set
    followup_template_id: str = os.getenv("BIRD_FOLLOWUP_TEMPLATE_ID", "")  # Bird project id of the approved thank-you template; blank = follow-up disabled
    followup_template_version: str = os.getenv("BIRD_FOLLOWUP_TEMPLATE_VERSION", "latest")
    followup_daily_cap: int = int(os.getenv("FOLLOWUP_DAILY_CAP", "100"))


def load_config() -> DidacConfig:
    return DidacConfig()


def brochure_catalog(config: DidacConfig) -> Dict[str, dict[str, str]]:
    return {
        "1": {"key": "sia", "name": "SIA", "url": config.sia_brochure_url or config.brochure_url},
        "2": {"key": "tiqer", "name": "TiQer", "url": config.tiqer_brochure_url},
        "3": {"key": "play_lab", "name": "Play Lab", "url": config.play_lab_brochure_url},
        "4": {"key": "stream", "name": "STREAM", "url": config.stream_brochure_url},
        "5": {
            "key": "lab_in_classroom",
            "name": "Lab-in-Classroom",
            "url": config.lab_in_classroom_brochure_url or config.brochure_url,
        },
    }
