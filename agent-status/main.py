import argparse
import asyncio
import sys

from agent_status import main as agent_status_main
from roadmap_parser import print_roadmap_status

def main():
    parser = argparse.ArgumentParser(description="Homelab Agent Status CLI")
    parser.add_argument("--roadmap", action="store_true", help="Print structured roadmap progress")
    
    args = parser.parse_args()
    
    if args.roadmap:
        print_roadmap_status()
    else:
        asyncio.run(agent_status_main())

if __name__ == "__main__":
    main()
