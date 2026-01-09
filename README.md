# LLamaScanner

A Python command-line utility to automatically discover distributed Ollama instances across a **Tailscale** network, inventory their available models, and benchmark their inference speed.

The tool generates a clean routing table identifying the fastest host for each model in your private cloud to facilitate the distribution and load balancing of local AI models.

## Why LlamaScanner?

While many tools exist for load balancing or benchmarking LLMs, most require manual configuration or assume heavy infrastructure. This utility fills a specific gap for home labs:

1.  **Dynamic Discovery for Static Proxies:** While robust proxies like `ollama_proxy_server` or Nginx typically rely on static configuration files, LlamaScanner bridges the gap by using the **Tailscale API** to automatically discover new nodes. It can generate the necessary configuration for these proxies, allowing them to adapt instantly when you add a new laptop or GPU server to your network without manual editing.
2.  **Model-Aware Routing:** It doesn't just check if a host is "up"; it inventories *which* models are pulled on which machine, enabling rapid experimentation without manually updating config files.
3.  **Real-World Benchmarks:** Instead of relying on theoretical specs, **LlamaScanner** runs live inference tests under your specific network conditions, accounting for Wi-Fi latency and thermal throttling of the host machine.

## Features

- **Auto-Discovery:** Scans the local Tailscale network map to find active peers (100.x.x.x) without brute-force scanning.
- **Inventory Mapping:** Connects to every active node to retrieve the list of pulled models (`/api/tags`).
- **Proxy Configuration:** Generates a `targets.json` file compatible with `ollama_proxy_server` for instant load balancer updates.
- **Robust Benchmarking:**
  - Sequential testing per host to prevent VRAM thrashing.
  - "Warm-up" requests to ensure models are loaded into memory before measuring.
  - Stabilized metrics using fixed seeds, `temperature=0`, and multiple sample averaging.
- **Data Export:** Outputs a clean CSV via CLI or returns a formatted Pandas DataFrame.

## Prerequisites

### 1. Tailscale
This script relies on the `tailscale` CLI to discover peers. Ensure Tailscale is installed and authenticated on the machine running this script.

### 2. Network Reachability
The script must be able to reach port `11434` on your remote Tailscale nodes.

* **Verification:** You can test this by running `curl http://<TAILSCALE_IP>:11434` from your central node.

### 3. Python Requirements
```bash
pip install requests pandas
```

## Usage

Run the script from the command line periodically following changes to local LLM installs or hardware configurations. By default it will print the routing table to the console.

```bash
python LlamaScanner.py
```

### Command Line Arguments

| Argument | Default | Description |
| :--- | :--- | :--- |
| `-o`, `--output` | None | Path to save the routing table as a CSV file (e.g., `routes.csv`). |
| `--proxy` | None | Path to save a JSON config for `ollama_proxy_server` (e.g., `targets.json`). |
| `--runs` | 3 | Number of benchmark inference passes to average per model. |
| `--cooldown` | 5 | Seconds to wait between models on the same host (clears VRAM). |
| `--workers` | 10 | Max number of hosts to benchmark concurrently. |
| `--timeout-load` | 300 | Max seconds to wait for a model to load into VRAM (cold start). |
| `--timeout-bench`| 60 | Max seconds to wait for generation to complete. |

### Examples

**Save the routing table to a file:**
```bash
python LlamaScanner.py --output my_routes.csv
```

**Generate a proxy config (Skipping benchmarks for speed):**
```bash
python LlamaScanner.py --proxy targets.json
```

**Run a more rigorous benchmark (5 runs per model):**
```bash
python LlamaScanner.py --runs 5 --cooldown 10
```

## Automation with ollama_proxy_server

You can use **LlamaScanner** as a dynamic "Control Plane" for your load balancer. Set up a cron job to run the script periodically; this ensures your proxy always knows about new nodes or new models.

```bash
# 1. Discover nodes & generate config (Fast - skips benchmarks)
python LlamaScanner.py --proxy targets.json

# 2. Restart your proxy to apply changes (Example command)
# systemctl restart ollama-proxy
```

## Example Output

```text
[-] Scanning Tailscale network for active nodes...
[-] Inventorying 3 hosts...
[-] Starting Distributed Benchmarks...
    -> Benchmarking 100.101.102.103 (3 models)...
    -> Benchmarking 100.80.50.10 (1 models)...

--- Routing Table ---
         model                 route                       ip    tps
      gemma:7b      ollama/gemma:7b  [http://100.101.102.103:11434](http://100.101.102.103:11434)  52.00
 mistral:latest  ollama/mistral:latest  [http://100.101.102.103:11434](http://100.101.102.103:11434)  48.10
 llama3:latest   ollama/llama3:latest  [http://100.101.102.103:11434](http://100.101.102.103:11434)  45.20
      gemma:2b      ollama/gemma:2b     [http://100.80.50.10:11434](http://100.80.50.10:11434)  34.50

[+] Saved routing table to: my_routes.csv
```

## How It Works

1. **Discovery:** The script runs `tailscale status --json` to get a list of known peers. It filters for IPv4 addresses in the `100.x` range and verifies port `11434` connectivity via TCP.
2. **Inventory:** It queries `/api/tags` on every active host concurrently to build a map of `{IP: [Model List]}`.
3. **Benchmarking:**
   - *Note: This step is skipped if only `--proxy` is used.*
   - It iterates through the inventory. Hosts are processed in parallel, but models *within* a host are processed sequentially.
   - It sends a standard prompt ("Write me a python function to identify primes that rhyme.") with `num_predict=100` and `seed=1337`.
   - It calculates the exact **Tokens Per Second** using Ollama's internal `eval_duration` metric (pure compute time).
4. **Reporting:** Results are aggregated into a routing table, sorted by model and speed.
