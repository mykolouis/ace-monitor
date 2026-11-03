import os
import platform
import socket
import subprocess
import sys
import time
import GPUtil
import psutil
import requests

# CONFIGURATION: Target server host IP and port
SERVER_IP = "10.39.1.12"
PORT = 5000
INTERVAL = 10

# Endpoints
SPECS_URL = f"http://{SERVER_IP}:{PORT}/api/specs"
REPORT_URL = f"http://{SERVER_IP}:{PORT}/api/report"

# Global variables for network speed calculation delta tracking
_last_net_io = None
_last_net_time = None


def get_gpu_model():
    """Retrieves the specific hardware model name for the first available NVIDIA GPU."""
    try:
        gpus = GPUtil.getGPUs()
        if gpus and len(gpus) > 0:
            return gpus[0].name
    except Exception:
        pass  
    return "Unknown GPU"


def get_gpu_live_metrics():
    """Retrieves live utilization usage and temperature for the first available NVIDIA GPU."""
    try:
        gpus = GPUtil.getGPUs()
        if gpus and len(gpus) > 0:
            gpu = gpus[0]
            return int(gpu.load * 100), gpu.temperature
    except Exception:
        pass  
    return 0, None


def parse_lhm_tree(node, metrics, specs=None):
    """Recursively parses Libre Hardware Monitor JSON tree for temperature and model."""
    if not isinstance(node, dict):
        return

    text = str(node.get("Text", ""))
    image = str(node.get("Image", ""))
    value_str = str(node.get("Value", "") or "")
    sensor_type = str(node.get("SensorType", ""))
    text_lower = text.lower()

    if image in ("intel", "amd") or "intel" in text_lower or "amd" in text_lower:
        if "control" not in text_lower and specs is not None:
            specs["processor_model"] = text

    if sensor_type == "Temperature" or "°c" in value_str.lower() or "Â°c" in value_str.lower():
        try:
            clean_temp = float(
                value_str.replace(" °C", "")
                         .replace("°C", "")
                         .replace("Â°C", "")
                         .strip()
            )
            
            if any(kw in text_lower for kw in ["cpu", "core", "package", "tdie", "processor", "ccd", "cpu core"]):
                if "gpu" not in text_lower:
                    metrics["cpu_temp"] = clean_temp
            elif "temperature" in text_lower and metrics["cpu_temp"] is None:
                metrics["cpu_temp"] = clean_temp
        except ValueError:
            pass

    for key in ("Children", "children", "SubNodes"):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                parse_lhm_tree(child, metrics, specs)


def get_cpu_temperature():
    """Fetches the live JSON tree from Libre Hardware Monitor's local web server."""
    try:
        response = requests.get("http://localhost:8085/data.json", timeout=2)
        if response.status_code == 200:
            data = response.json()
            metrics = {"cpu_temp": None}
            specs = {}
            
            parse_lhm_tree(data, metrics, specs)
            return metrics.get("cpu_temp")
    except Exception:
        pass
    return None


def get_exact_processor_name():
    """Retrieves the precise marketing name of the CPU across Windows, Linux, and macOS."""
    os_type = platform.system()
    
    if os_type == "Windows":
        try:
            import wmi
            w = wmi.WMI()
            for cpu in w.Win32_Processor():
                return cpu.Name.strip()
        except Exception:
            pass
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            processor_name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return processor_name.strip()
        except Exception:
            pass

    elif os_type == "Linux":
        try:
            command = "cat /proc/cpuinfo | grep 'model name' | uniq"
            output = subprocess.check_output(command, shell=True).decode().strip()
            if ":" in output:
                return output.split(":")[1].strip()
        except Exception:
            pass

    elif os_type == "Darwin":
        try:
            command = "sysctl -n machdep.cpu.brand_string"
            return subprocess.check_output(command, shell=True).decode().strip()
        except Exception:
            pass

    return platform.processor()


def get_ram_stick_details():
    """Retrieves total physical RAM stick count and individual clock speeds (MHz)."""
    os_type = platform.system()
    stick_speeds = []
    
    if os_type == "Windows":
        try:
            import wmi
            w = wmi.WMI()
            for memory in w.Win32_PhysicalMemory():
                speed = getattr(memory, "Speed", None)
                if speed:
                    stick_speeds.append(f"{speed} MHz")
        except Exception:
            pass

    elif os_type == "Linux":
        try:
            command = "sudo dmidecode --type 17 | grep 'Speed:' | grep -v 'Unknown'"
            output = subprocess.check_output(command, shell=True).decode().strip()
            for line in output.split("\n"):
                if "Speed:" in line:
                    speed_val = line.split(":")[1].strip()
                    stick_speeds.append(speed_val)
        except Exception:
            pass

    elif os_type == "Darwin":
        try:
            command = "system_profiler SPMemoryDataType | grep 'Speed:'"
            output = subprocess.check_output(command, shell=True).decode().strip()
            for line in output.split("\n"):
                if "Speed:" in line:
                    speed_val = line.split(":")[1].strip()
                    stick_speeds.append(speed_val)
        except Exception:
            pass

    stick_count = len(stick_speeds) if stick_speeds else 1
    speeds_list = stick_speeds if stick_speeds else ["Unknown Speed"]
    
    return stick_count, speeds_list


def get_disk_usage():
    """Scans active disk partitions and returns their percentage usage."""
    disk_stats = {}
    try:
        partitions = psutil.disk_partitions(all=False)
        for partition in partitions:
            if "loop" in partition.device or not partition.mountpoint:
                continue

            if platform.system() == "Windows" and not partition.fstype:
                continue

            try:
                usage = psutil.disk_usage(partition.mountpoint)
                drive_name = partition.device.replace(":\\", "").replace("/dev/", "").upper()
                if not drive_name or drive_name == "":
                    drive_name = "ROOT"
                disk_stats[drive_name] = int(usage.percent)
            except (PermissionError, FileNotFoundError, OSError):
                continue
    except Exception:
        pass

    if not disk_stats:
        try:
            disk_stats["C"] = int(psutil.disk_usage("/").percent)
        except Exception:
            disk_stats["Drive"] = 0
            
    return disk_stats


def get_network_speed():
    """Calculates live network upload and download speed in KB/s based on delta bytes over time."""
    global _last_net_io, _last_net_time
    try:
        current_io = psutil.net_io_counters()
        current_time = time.time()
        
        if _last_net_io is None or _last_net_time is None:
            _last_net_io = current_io
            _last_net_time = current_time
            return {"upload_kb_s": 0.0, "download_kb_s": 0.0}
            
        time_delta = current_time - _last_net_time
        if time_delta <= 0:
            time_delta = 1
            
        sent_delta = current_io.bytes_sent - _last_net_io.bytes_sent
        recv_delta = current_io.bytes_recv - _last_net_io.bytes_recv
        
        _last_net_io = current_io
        _last_net_time = current_time
        
        upload_speed = round((sent_delta / 1024) / time_delta, 2)
        download_speed = round((recv_delta / 1024) / time_delta, 2)
        
        return {
            "upload_kb_s": upload_speed,
            "download_kb_s": download_speed
        }
    except Exception:
        pass
    return {"upload_kb_s": 0.0, "download_kb_s": 0.0}


def collect_static_specs():
    """Gathers permanent hardware capabilities sent only once or upon server request."""
    hostname = platform.node()
    ram_mem = psutil.virtual_memory()
    
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
    except Exception:
        ip_address = "127.0.0.1"

    cpu_cores_physical = psutil.cpu_count(logical=False) or 0
    cpu_threads_logical = psutil.cpu_count(logical=True) or 0
    ram_stick_count, ram_speeds = get_ram_stick_details()

    specs_payload = {
        "hostname": hostname,
        "ip_address": ip_address,
        "operating_system": f"{platform.system()} {platform.release()}",
        "processor_model": get_exact_processor_name(),
        "cpu_physical_cores": cpu_cores_physical,
        "cpu_logical_threads": cpu_threads_logical,
        "gpu_model": get_gpu_model(),
        "total_ram": f"{round(ram_mem.total / (1024**3), 2)} GB",
        "ram_stick_count": ram_stick_count,
        "ram_speeds": ram_speeds
    }
    return specs_payload


def collect_live_metrics():
    """Gathers running system statistics that fluctuate actively."""
    hostname = platform.node()
    gpu_load, gpu_temp = get_gpu_live_metrics()
    cpu_percent = int(psutil.cpu_percent(interval=0.1))
    ram_percent = int(psutil.virtual_memory().percent)
    
    cpu_temp_value = get_cpu_temperature()
    disks = get_disk_usage()
    network_speed = get_network_speed()

    metrics_payload = {
        "hostname": hostname,
        "cpu": cpu_percent,
        "ram": ram_percent,
        "gpu": gpu_load,
        "cpu_temp": cpu_temp_value,
        "gpu_temp": gpu_temp,
        "disks": disks,
        "network": network_speed,
    }
    return metrics_payload


def is_admin():
    """Checks if the script is currently running with Administrator/Root privileges."""
    try:
        return os.getuid() == 0
    except AttributeError:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0


def main():
    if platform.system() == "Windows" and not is_admin():
        print("[WARNING] Script is not running as Administrator!")
        print("[WARNING] RAM details query requires Administrator privileges.")
        print("-" * 60)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "System-Monitor-Agent/2.0"
    }

    print("Gathering hardware profile specs...")
    specs_data = collect_static_specs()
    
    while True:
        try:
            print(f"Registering hardware layout profile to API: {SPECS_URL}...")
            response = requests.post(SPECS_URL, json=specs_data, headers=headers, timeout=4)
            if response.status_code == 200:
                print("Hardware layout baseline registered successfully.")
                break
            else:
                print(f"Server rejected profile structure configuration with code: {response.status_code}. Retrying...")
        except requests.exceptions.RequestException:
            print("Server registration offline. Retrying specifications baseline assignment...")
        time.sleep(5)

        print(f"Monitoring Agent streaming initialized. Directing live cycles to {REPORT_URL}...")
        psutil.cpu_percent(interval=None)

    while True:
        try:
            metrics_payload = collect_live_metrics()
            response = requests.post(REPORT_URL, json=metrics_payload, headers=headers, timeout=2)
            
            if response.status_code == 200:
                res_data = response.json()
                
                # If the server lost memory and requests specs back, re-send them automatically!
                if res_data.get("request_specs", False):
                    print("[INFO] Server reported missing specs. Re-registering static specs...")
                    fresh_specs = collect_static_specs()
                    requests.post(SPECS_URL, json=fresh_specs, headers=headers, timeout=4)
                else:
                    print(f"Live telemetry pack uploaded successfully at {time.strftime('%H:%M:%S')} (CPU Temp: {metrics_payload.get('cpu_temp')}°C)")
            else:
                print(f"Live telemetry packaging dropped by server with code: {response.status_code}")
        except requests.exceptions.RequestException:
            print("Dashboard communications line failed. Re-attempting pipeline execution link...")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()