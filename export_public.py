"""Compatibility entry point for the cloud site's strictly allowlisted export."""
from pathlib import Path
from sync import ROOT, build_site

if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("destination",type=Path)
    args=parser.parse_args()
    build_site(ROOT,args.destination)
