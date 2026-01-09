import socket
import subprocess
import json
import concurrent.futures
import requests
import time
import statistics
import argparse
import sys

import pandas as pd

from collections import defaultdict

# Defaults
DEFAULT_LOAD_TIMEOUT = 300
DEFAULT_BENCH_TIMEOUT = 60
DEFAULT_COOLDOWN = 5
DEFAULT_RUNS = 3


def get_args():
    parser = argparse.ArgumentParser(
        description="Ollama Tailscale Cluster Discovery & Benchmark Tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument("-o", "--output", help="Path to save the routing table (CSV)")
    parser.add_argument("-p", "--proxy", help="Path to save a JSON config for ollama_proxy_server")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="Number of benchmark runs per model")
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="Seconds to wait between models")
    parser.add_argument("--timeout-load", type=int, default=DEFAULT_LOAD_TIMEOUT, help="Seconds to wait for model load")
    parser.add_argument("--timeout-bench", type=int, default=DEFAULT_BENCH_TIMEOUT, help="Seconds to wait for generation")
    parser.add_argument("--workers", type=int, default=10, help="Max concurrent hosts to benchmark")
    
    return parser.parse_args()


    
def getTailscaleIps():
    """Retrieves unique IPv4 addresses (100.x.x.x) from Tailscale CLI."""
    ips = set()
    try:
        cmd = ["tailscale", "status", "--json"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            nodes = [data.get("Self", {})] + list(data.get("Peer", {}).values())
            for node in nodes:
                valid_ips = [
                    ip for ip in node.get("TailscaleIPs", []) 
                    if ":" not in ip and ip.startswith("100.")
                ]
                ips.update(valid_ips)
    except FileNotFoundError:
        print("[!] Error: 'tailscale' command not found. Is it installed?")
    except Exception as e:
        print(f"[!] Error querying Tailscale: {e}")
    return list(ips)



def checkOllamaPort(ip, port=11434, timeout=1.0):
    """Checks if port 11434 is open."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if sock.connect_ex((ip, port)) == 0:
                return ip
    except:
        pass
    return None



def findActiveHosts():
    """Scans Tailscale IPs for active Ollama instances."""
    print("[-] Scanning Tailscale network for active nodes...")
    candidates = getTailscaleIps()
    activeHosts = []
    
    if not candidates:
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(checkOllamaPort, ip): ip for ip in candidates}
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                activeHosts.append(future.result())
    return activeHosts


    
def getModelsOnHost(ip, port=11434):
    """Gets list of models from a host."""
    try:
        resp = requests.get(f"http://{ip}:{port}/api/tags", timeout=5)
        resp.raise_for_status()
        return ip, [m['name'] for m in resp.json().get('models', [])]
    except:
        return ip, []



def mapNetwork(hosts):
    """Maps hosts to their installed models."""
    print(f"[-] Inventorying {len(hosts)} hosts...")
    inventory = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(getModelsOnHost, ip): ip for ip in hosts}
        for future in concurrent.futures.as_completed(futures):
            ip, models = future.result()
            if models:
                inventory[ip] = models
    return inventory


    
def benchmarkSingleModel(ip, model, args, port=11434):
    """Runs stabilized benchmark."""
    url = f"http://{ip}:{port}/api/generate"
    payload = {
        "model": model, 
        "prompt": "Write a paragraph about the history of the internet.", 
        "stream": False, 
        "options": {"num_predict": 100, "temperature": 0.0, "seed": 42}
    }
    
    try:
        requests.post(url, json=payload, timeout=args.timeout_load)
    except:
        return "Failed (Warmup)"

    samples = []
    for _ in range(args.runs):
        try:
            resp = requests.post(url, json=payload, timeout=args.timeout_bench)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("eval_duration", 0) > 0:
                    tps = data["eval_count"] / (data["eval_duration"] / 1e9)
                    samples.append(tps)
        except:
            pass
            
    if not samples:
        return "Failed"
        
    return round(statistics.mean(samples), 2)



def processHostBenchmarks(ip, models, args):
    """Benchmarks models sequentially on a single host."""
    results = {}
    print(f"    -> Benchmarking {ip} ({len(models)} models)...")
    for model in models:
        tps = benchmarkSingleModel(ip, model, args)
        results[model] = tps
        time.sleep(args.cooldown)
    return ip, results


    
def runRobustBenchmarks(inventory, args):
    """Orchestrates distributed benchmarks."""
    print("[-] Starting Distributed Benchmarks...")
    final_stats = defaultdict(dict)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(processHostBenchmarks, ip, mods, args) for ip, mods in inventory.items()]
        for future in concurrent.futures.as_completed(futures):
            ip, data = future.result()
            final_stats[ip] = data
            
    return dict(final_stats)



def generateRoutingDataFrame(stats):
    """Generates a Pandas DataFrame from benchmark stats."""
    rows = []
    for ip, models in stats.items():
        for model, speed in models.items():
            if isinstance(speed, (float, int)):
                rows.append({
                    "model": model,
                    "route": f"ollama/{model}",
                    "ip": f"http://{ip}:11434",
                    "tps": speed
                })
    
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(by=["model", "tps"], ascending=[True, False])
        df = df[["model", "route", "ip", "tps"]]
    return df



def generateProxyJson(inventory):
    """Generates configuration JSON for proxy servers from Inventory."""
    targets = []
    for ip, models in inventory.items():
        if models:
            targets.append({
                "url": f"http://{ip}:11434",
                "models": models
            })
    return json.dumps({"targets": targets}, indent=2)


    
def main():
    args = get_args()
    results = dict()
    
    # 1. Discovery
    hosts = findActiveHosts()
    if not hosts:
        print("[!] No active Ollama hosts found on Tailscale.")
        sys.exit(1)
        
    # 2. Inventory
    inventory = mapNetwork(hosts)
    if not inventory:
        print("[!] Hosts found, but no models detected.")
        sys.exit(1)

    # 3. Proxy Generation (Fast Path)
    if args.proxy:
        proxyConfig = generateProxyJson(inventory)
        with open(args.proxy, "w") as f:
            f.write(proxyConfig)
        print(f"[+] Saved proxy config to: {args.proxy}")
        results['proxy'] = proxyConfig

    # 4. Benchmarking Logic
    # We skip benchmarks if the user ONLY asked for a proxy file and didn't ask for a CSV report
    # If the user supplied NO arguments, they probably want the console table, so we default to True.
    should_benchmark = args.output or (not args.output and not args.proxy)

    if should_benchmark:
        stats = runRobustBenchmarks(inventory, args)
        
        print("\n--- Routing Table ---")
        df = generateRoutingDataFrame(stats)
        
        if df.empty:
            print("[!] Benchmarks completed but no successful results found.")
        else:
            print(df.to_string(index=False))
            
            if args.output:
                df.to_csv(args.output, index=False)
                results['routing'] = df
                print(f"\n[+] Saved routing table to: {args.output}")
    else:
        print("[-] Skipping benchmarks (Proxy-only mode).")
    return results



if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Aborted by user.")
        sys.exit(0)
