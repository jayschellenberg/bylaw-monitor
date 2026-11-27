"""Regenerate the dashboard row files (data/dash/*.json) from current data, without a full weekly run."""
import register
import weekly
from common import read_json

if __name__ == "__main__":
    weekly.dashboard_files(read_json(register.REGISTER))
    print("wrote", weekly.DASH)
