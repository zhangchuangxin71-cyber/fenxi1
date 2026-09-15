"""
Simple monitoring tool for embedding service.

Usage:
    # Specify port (required)
    python -m embedding_service.monitor --port 8030
    
    # Specify custom host and port
    python -m embedding_service.monitor --host 192.168.1.100 --port 8031
    
    # Use full URL
    python -m embedding_service.monitor --url http://example.com:8030
    
    # Check once and exit
    python -m embedding_service.monitor --port 8030 --once
    
    # Custom monitoring interval
    python -m embedding_service.monitor --port 8030 --interval 5
"""
import argparse
import time
from datetime import datetime

import requests


def format_timestamp():
    """Format current timestamp."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_health(base_url: str) -> dict:
    """Get health status."""
    try:
        response = requests.get(f"{base_url}/health", timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        return {"error": str(exc)}


def get_stats(base_url: str) -> dict:
    """Get service statistics."""
    try:
        response = requests.get(f"{base_url}/stats", timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        return {"error": str(exc)}


def print_status(base_url: str):
    """Print current service status."""
    print(f"\n{'=' * 80}")
    print(f"Embedding Service Monitor - {format_timestamp()}")
    print(f"{'=' * 80}")
    
    # Health check
    health = get_health(base_url)
    if "error" in health:
        print(f"❌ Service is DOWN: {health['error']}")
        return
    
    print(f"✅ Service is UP")
    print(f"   Status: {health.get('status', 'unknown')}")
    print(f"   CLIP Device: {health.get('clip_device', 'unknown')}")
    print(f"   BGE Device: {health.get('bge_device', 'unknown')}")
    
    # Statistics
    stats = get_stats(base_url)
    if "error" not in stats:
        print(f"\n📊 Statistics:")
        print(f"   Version: {stats.get('version', 'unknown')}")
        
        tasks = stats.get('tasks', {})
        print(f"   Total Tasks: {tasks.get('total', 0)}")
        
        by_status = tasks.get('by_status', {})
        if by_status:
            print(f"   Task Status:")
            for status, count in by_status.items():
                print(f"      {status}: {count}")
        
        models = stats.get('models', {})
        print(f"   Models:")
        print(f"      CLIP Loaded: {models.get('clip_loaded', False)}")
        print(f"      BGE Loaded: {models.get('bge_loaded', False)}")


def monitor_loop(base_url: str, interval: int):
    """Monitor service in a loop."""
    print(f"Monitoring {base_url} every {interval} seconds")
    print("Press Ctrl+C to stop")
    
    try:
        while True:
            print_status(base_url)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Monitor embedding service")
    parser.add_argument(
        "--url",
        default=None,
        help="Service base URL (e.g., http://127.0.0.1:8030). If provided, --host and --port are ignored.",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=False,
        help="Service port (required if --url is not provided)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Service host (default: 127.0.0.1, ignored if --url is provided)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Monitoring interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print status once and exit",
    )
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.url is None:
        if args.port is None:
            parser.error("--port is required when --url is not provided")
        args.url = f"http://{args.host}:{args.port}"
    
    if args.once:
        print_status(args.url)
    else:
        monitor_loop(args.url, args.interval)


if __name__ == "__main__":
    main()
