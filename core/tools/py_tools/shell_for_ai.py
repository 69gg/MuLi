## -!- START REGISTER TOOL -!- ##
## -!- START TOOL DEFINITION -!- ##
TOOL_NAME = "shell_for_ai"
TOOL_DESCRIPTION = "A tool to interact with a persistent shell session inside a Docker container. Supports sending text input, special keys, and retrieving output."
TOOL_FUNCTIONS = ["send_shell_input", "get_shell_output", "restart_shell_session", "expose_container_port", "list_exposed_ports", "close_exposed_port"]
TOOL_PARAMETERS = [
    [
        {"input_text": "The text command to type into the shell. Optional if sending a key_combo."},
        {"key_combo": "Special key combination to send. Supported: 'Enter', 'Ctrl+C', 'Ctrl+Z', 'Ctrl+D', 'Up', 'Down'. Optional."}
    ],
    [
        {"timeout_seconds": "How long to wait for output gathering (not strict sleep, just read window). Default 1."}
    ],
    [],
    [
        {"container_port": "The port inside the container to expose."},
        {"host_port": "The port on the host to bind to. If 0 or omitted, a random free port will be selected."}
    ],
    [],
    [
        {"host_port": "The host port to close the forwarding for."}
    ]
]
## -!- END TOOL DEFINITION -!- ##

import os
import pty
import select
import subprocess
import time
import fcntl
import socket
import threading
import sys
from config_manage.manager import ConfigManager

# Global session state
SHELL_SESSION = {
    "master_fd": None,
    "process": None,
    "buffer": b"",
    "container_name": "ai_shell_container", # Default name
    "port_forwards": {} # host_port -> subprocess (Popen object)
}

config = ConfigManager("config.json")

def _get_config_value(key, default=None):
    # Try to get from config, handle potential structure mismatches safely
    try:
        val = config.get(f"tools_api_config.shell_for_ai.{key}")
        return val if val is not None else default
    except Exception:
        return default

def _ensure_session():
    """Ensures that the shell session is running. Starts it if necessary."""
    
    # Check if enabled
    enabled = _get_config_value("enable", False)
    if not enabled:
        return "Error: shell_for_ai is not enabled in config.json. Please set 'enable': true."

    container_name = _get_config_value("container_name", "ai_shell_container")
    SHELL_SESSION["container_name"] = container_name

    # Check if process is alive
    if SHELL_SESSION["process"] is not None:
        if SHELL_SESSION["process"].poll() is None:
            return None # Alive
        else:
            # Died, cleanup
            try:
                os.close(SHELL_SESSION["master_fd"])
            except:
                pass
            SHELL_SESSION["master_fd"] = None
            SHELL_SESSION["process"] = None
    
    # Start new session
    # Verify container exists/running first? 
    # Let's just try running docker exec. If it fails, capturing stderr would be good.
    # But pty merges stdout/stderr.
    
    # Check if container is running
    check_cmd = ["docker", "ps", "-q", "-f", f"name={container_name}"]
    try:
        if not subprocess.check_output(check_cmd).strip():
             return f"Error: Docker container '{container_name}' is not running. Please deploy it first."
    except subprocess.CalledProcessError:
        return "Error: Failed to check Docker container status. Is Docker installed and running?"

    master, slave = pty.openpty()
    
    # Start the process
    # We use 'env TERM=xterm' to ensure good behavior
    cmd = ["docker", "exec", "-it", container_name, "env", "TERM=xterm", "bash"]
    
    try:
        p = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True
        )
        os.close(slave) # Close child's handle in parent
        
        SHELL_SESSION["process"] = p
        SHELL_SESSION["master_fd"] = master
        
        # Set non-blocking
        fl = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        
        time.sleep(1) # Wait for prompt
        _read_available_output() # Clear initial output
        return None
    except Exception as e:
        if master:
            os.close(master)
        return f"Error starting shell session: {str(e)}"

def _read_available_output():
    """Reads all currently available output from the master_fd."""
    if SHELL_SESSION["master_fd"] is None:
        return
    
    try:
        while True:
            r, _, _ = select.select([SHELL_SESSION["master_fd"]], [], [], 0.01)
            if not r:
                break
            chunk = os.read(SHELL_SESSION["master_fd"], 10240)
            if not chunk:
                break
            SHELL_SESSION["buffer"] += chunk
    except OSError:
        pass

def send_shell_input(input_text: str = None, key_combo: str = None) -> str:
    """
    Sends text or a key combination to the shell session.
    Example: send_shell_input("python3") then send_shell_input(key_combo="Enter")
    """
    err = _ensure_session()
    if err:
        return err

    fd = SHELL_SESSION["master_fd"]
    
    msg = []

    if input_text:
        os.write(fd, input_text.encode('utf-8'))
        msg.append(f"Sent text: {input_text}")

    if key_combo:
        key_map = {
            "Enter": b"\n",
            "Return": b"\n",
            "Ctrl+C": b"\x03",
            "Ctrl+Z": b"\x1a",
            "Ctrl+D": b"\x04",
            "Up": b"\x1b[A",
            "Down": b"\x1b[B",
            "Tab": b"\t"
        }
        
        # Simple parsing for other Ctrl+X
        if key_combo.startswith("Ctrl+") and len(key_combo) == 6:
            char = key_combo[5].lower()
            if 'a' <= char <= 'z':
                code = bytes([ord(char) - 96])
                os.write(fd, code)
                msg.append(f"Sent key: {key_combo}")
            else:
                 msg.append(f"Unknown Ctrl key: {key_combo}")
        elif key_combo in key_map:
            os.write(fd, key_map[key_combo])
            msg.append(f"Sent key: {key_combo}")
        else:
            msg.append(f"Unknown key: {key_combo}")

    # Read output immediately locally to update buffer
    time.sleep(0.1)
    _read_available_output()
    
    return ", ".join(msg) if msg else "No input provided."

def get_shell_output(timeout_seconds: int = 1) -> str:
    """
    Retrieves the output from the shell session.
    """
    try:
        timeout_seconds = float(timeout_seconds)
    except (ValueError, TypeError):
        timeout_seconds = 1.0

    err = _ensure_session()
    if err:
        return err
        
    # Wait a bit if requested
    if timeout_seconds > 0:
        time.sleep(timeout_seconds)
        
    _read_available_output()
    
    # Return buffer and clear it?
    # Usually better to clear it so we don't repeat history.
    output = SHELL_SESSION["buffer"]
    SHELL_SESSION["buffer"] = b""
    
    # Decode 
    try:
        decoded = output.decode('utf-8', errors='replace')
        # Filter out some terminal control sequences if necessary, but raw is okay for now.
        return decoded
    except Exception as e:
        return f"<Decoding Error: {str(e)}>"

def restart_shell_session() -> str:
    """Forces a restart of the Docker shell session."""
    if SHELL_SESSION["process"]:
        try:
            SHELL_SESSION["process"].terminate()
            SHELL_SESSION["process"].wait()
        except:
            pass
        try:
            os.close(SHELL_SESSION["master_fd"])
        except:
            pass
            
    SHELL_SESSION["process"] = None
    SHELL_SESSION["master_fd"] = None
    SHELL_SESSION["buffer"] = b""
    
    return "Session terminated. It will restart on next input."

def _get_container_ip(container_name: str) -> str:
    """Gets the IP address of the container."""
    try:
        cmd = ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container_name]
        return subprocess.check_output(cmd).decode('utf-8').strip()
    except subprocess.CalledProcessError:
        return None

def _find_free_port() -> int:
    """Finds a free port on the host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def expose_container_port(container_port: int, host_port: int = 0) -> str:
    """
    Exposes a port from the container to the host using a python-based proxy.
    """
    try:
        container_port = int(container_port)
        host_port = int(host_port) if host_port else 0
    except ValueError:
        return "Error: Ports must be integers."

    err = _ensure_session()
    if err:
        return err

    container_name = SHELL_SESSION["container_name"]
    container_ip = _get_container_ip(container_name)
    
    if not container_ip:
        return f"Error: Could not determine IP for container '{container_name}'."

    if host_port == 0:
        host_port = _find_free_port()
    
    if host_port in SHELL_SESSION["port_forwards"]:
        return f"Error: Host port {host_port} is already being forwarded."

    # Python one-liner for TCP proxy
    # We use a simple threaded proxy script to handle connections
    proxy_script = f"""
import socket, threading, sys

def forward(source, destination):
    try:
        string = ' '
        while string:
            string = source.recv(4096)
            if string:
                destination.sendall(string)
            else:
                try: source.shutdown(socket.SHUT_RD)
                except: pass
                try: destination.shutdown(socket.SHUT_WR)
                except: pass
    except:
        pass

def main():
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('0.0.0.0', {host_port}))
        server.listen(5)
        sys.stdout.write("READY\\n")
        sys.stdout.flush()
        while True:
            client, _ = server.accept()
            target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                target.connect(('{container_ip}', {container_port}))
                threading.Thread(target=forward, args=(client, target), daemon=True).start()
                threading.Thread(target=forward, args=(target, client), daemon=True).start()
            except Exception:
                client.close()
    except Exception:
        pass
main()
"""
    try:
        # Start the proxy process
        proc = subprocess.Popen(
            [sys.executable, "-c", proxy_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL
        )
        
        # Wait for "READY" signal or failure
        try:
             # Wait up to 2 seconds for the server to bind
            out = proc.stdout.read1(10) # type: ignore
            if b"READY" not in out:
                 # Check if process is dead
                if proc.poll() is not None:
                     return f"Error: Port forwarder failed to start. Port {host_port} might be in use."
        except Exception:
            pass

        SHELL_SESSION["port_forwards"][host_port] = {
            "process": proc,
            "container_port": container_port,
            "container_ip": container_ip
        }
        
        return f"Successfully exposed Container:{container_port} -> Host:{host_port}"

    except Exception as e:
        return f"Error starting port forwarder: {str(e)}"

def list_exposed_ports() -> str:
    """Lists all currently active port forwards."""
    if not SHELL_SESSION["port_forwards"]:
        return "No ports currently exposed."
    
    lines = ["Active Port Forwards:"]
    to_remove = []
    
    for hp, info in SHELL_SESSION["port_forwards"].items():
        proc = info["process"]
        if proc.poll() is not None:
            to_remove.append(hp)
            status = "(Process Died)"
        else:
            status = "(Active)"
        
        lines.append(f"  Host:{hp} -> Container:{info['container_port']} ({info['container_ip']}) {status}")
    
    # Cleanup dead processes
    for hp in to_remove:
        del SHELL_SESSION["port_forwards"][hp]
        
    return "\n".join(lines)

def close_exposed_port(host_port: int) -> str:
    """Stops the port forwarding for the specified host port."""
    try:
        host_port = int(host_port)
    except ValueError:
        return "Error: Host port must be an integer."

    if host_port not in SHELL_SESSION["port_forwards"]:
        return f"Error: No active forwarding found on host port {host_port}."
    
    info = SHELL_SESSION["port_forwards"][host_port]
    proc = info["process"]
    
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass
        
    del SHELL_SESSION["port_forwards"][host_port]
    return f"Port forwarding on host port {host_port} stopped."


## -!- END REGISTER TOOL -!- ##
