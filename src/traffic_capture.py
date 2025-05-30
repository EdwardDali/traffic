import subprocess
import socket
import re
import os
import json
import time
import threading
import logging
import random
from collections import deque, defaultdict
import capture_fields

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('traffic_capture')

class TrafficCaptureEngine:
    """Handles traffic capture and basic protocol information extraction"""
    
    def __init__(self, gui):
        """Initialize the traffic capture engine"""
        self.gui = gui  # Reference to the GUI to update status and logs
        self.running = False
        self.capture_thread = None
        self.tshark_process = None
        self.packet_queue = deque()
        self.alerts_by_ip = defaultdict(set)
        self.packet_batch_count = 0
        self.packet_count = 0
        self.packet_sample_count = 0  # For debug packet sampling
        
        # Use the database manager from the GUI
        self.db_manager = gui.db_manager
        
        # Reference to analysis manager (for advanced analysis)
        self.analysis_manager = getattr(gui, 'analysis_manager', None)
        
        # Create logs directory if it doesn't exist
        self.logs_dir = os.path.join(gui.app_root, "logs", "packets")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.gui.update_output(f"Packet samples will be saved to {self.logs_dir}")
    
    def get_interfaces(self):
        """Get network interfaces using tshark directly"""
        interfaces = []
        try:
            cmd = ["tshark", "-D"]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8', errors='replace')
            
            for line in output.splitlines():
                if not line.strip():
                    continue
                    
                # Parse tshark interface line which has format: NUMBER. NAME (DESCRIPTION)
                match = re.match(r'(\d+)\.\s+(.+?)(?:\s+\((.+)\))?$', line)
                if match:
                    idx, iface_id, desc = match.groups()
                    desc = desc or iface_id  # Use ID as description if none provided
                    
                    # Get IP address if possible
                    ip_addr = self.get_interface_ip(iface_id)
                    
                    # Add to interfaces list (name, id, ip, description)
                    # name and description are for display, id is for tshark
                    interfaces.append((desc, iface_id, ip_addr, desc))
                    
            return interfaces
        except subprocess.CalledProcessError as e:
            self.gui.update_output(f"Error getting tshark interfaces: {e.output.decode('utf-8', errors='replace')}")
            return []
        except Exception as e:
            self.gui.update_output(f"Error listing interfaces: {e}")
            return []

    def get_interface_ip(self, interface_id):
        """Try to get the IP address for an interface"""
        try:
            # Check for an IPv4 address at the end of the interface name
            ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', interface_id)
            if ip_match:
                return ip_match.group(1)
            
            # Try to get IP using socket if possible
            try:
                # This method only works for named interfaces, not for interface IDs
                # that are numeric or GUIDs
                if not re.match(r'^\d+$', interface_id) and not re.match(r'^{.*}$', interface_id):
                    # Remove any trailing numbers that might be part of the tshark interface name
                    clean_name = re.sub(r'\d+$', '', interface_id)
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    # Try to get the IP of this interface by connecting to a dummy address
                    s.connect(('10.254.254.254', 1))
                    ip = s.getsockname()[0]
                    s.close()
                    return ip
            except:
                pass
                
            # For Windows adapters with GUIDs, we can't easily determine the IP
            # A more robust approach would use ipconfig or equivalent
            return "Unknown"
        except Exception:
            return "Unknown"
    
    def start_capture(self, interface, batch_size, sliding_window_size):
        """Start capturing packets on the specified interface"""
        if self.running:
            return
        
        self.running = True
        self.batch_size = batch_size
        self.sliding_window_size = sliding_window_size
        self.packet_count = 0
        self.packet_batch_count = 0
        self.packet_sample_count = 0
        self.capture_thread = threading.Thread(target=self.capture_packets, 
                                              args=(interface,), 
                                              daemon=True)
        self.capture_thread.start()
    
    def stop_capture(self):
        """Stop the packet capture"""
        self.running = False
        if self.tshark_process:
            try:
                self.tshark_process.terminate()
                self.tshark_process = None
            except Exception as e:
                self.gui.update_output(f"Error stopping tshark: {e}")
        
        if self.capture_thread:
            self.capture_thread.join(timeout=5)
            self.capture_thread = None
    
    def save_packet_sample(self, packet_data, packet_type="unknown"):
        """Save a sample packet to the logs folder for debugging"""
        try:
            # Only save up to 20 samples to avoid filling disk
            if self.packet_sample_count >= 20:
                return
                
            self.packet_sample_count += 1
            timestamp = int(time.time())
            filename = f"packet_{packet_type}_{timestamp}_{self.packet_sample_count}.json"
            filepath = os.path.join(self.logs_dir, filename)
            
            with open(filepath, 'w') as f:
                json.dump(packet_data, f, indent=2)
                
            self.gui.update_output(f"Saved {packet_type} packet sample to {filename}")
        except Exception as e:
            self.gui.update_output(f"Error saving packet sample: {e}")
    
    def capture_packets(self, interface):
        """Capture packets with streaming EK format parser using dynamic field definitions"""
        try:
            self.gui.update_output(f"Capturing on interface: {interface}")
            
            # Build tshark command dynamically from field definitions
            cmd = [
                "tshark",
                "-i", interface,
                "-T", "ek",  # Elasticsearch Kibana format
            ]
            
            # Add all fields from configuration
            for field in capture_fields.get_tshark_fields():
                cmd.extend(["-e", field])
            
            # Add filters
            cmd.extend([
                "-f", "tcp or udp or icmp or arp",  # Capture filter
                "-Y", "http or tls or ssl or http2 or dns or icmp or arp",  # Display filter
                "-l"  # Line-buffered output
            ])
            
            self.gui.update_output(f"Running command: {' '.join(cmd)}")
            
            # Start tshark process - use binary mode instead of text mode
            self.tshark_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            buffer = ""  # Buffer to accumulate EK output
            last_buffer_log_time = time.time()
            
            # Process each line from tshark
            for binary_line in iter(self.tshark_process.stdout.readline, b''):
                if not self.running:
                    break
                    
                # Decode with error handling
                line = binary_line.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                
                # Add line to buffer
                buffer += line + "\n"  # Add newline to keep lines separated
                
                # Log buffer size occasionally (not more than once every 30 seconds)
                current_time = time.time()
                if current_time - last_buffer_log_time > 30:
                    self.gui.update_output(f"Buffer size: {len(buffer)} chars")
                    last_buffer_log_time = current_time
                
                # Extract complete JSON objects from the buffer
                packet_objects = self.extract_ek_objects(buffer)
                if packet_objects:
                    # Only log this for large batches (more than 10 objects)
                    if len(packet_objects) > 10:
                        self.gui.update_output(f"Found {len(packet_objects)} complete JSON objects")
                    
                    # Process each packet JSON object
                    for packet_json in packet_objects:
                        try:
                            packet_data = json.loads(packet_json)
                            # Save a sample of each packet type
                            if random.random() < 0.05 and self.packet_sample_count < 20:
                                # Determine packet type for better sample naming
                                packet_type = self.determine_packet_type(packet_data)
                                self.save_packet_sample(packet_data, packet_type)
                                
                            # Process packet with simplified protocol handling (store basic data)
                            processed = self.process_packet_ek(packet_data)
                            
                            # Forward packet to analysis_manager for advanced analysis
                            if processed and self.analysis_manager:
                                self.analysis_manager.receive_packet_data(packet_data)
                                
                            self.packet_count += 1
                            self.packet_batch_count += 1
                        except json.JSONDecodeError as e:
                            self.gui.update_output(f"JSON Decode Error: {e}")
                    
                    # Remove processed content from buffer
                    # Keep only the last 1000 characters to handle any incomplete objects
                    buffer = buffer[-1000:] if len(buffer) > 1000 else buffer
                    
                    # Commit database changes in batches
                    if self.packet_batch_count >= self.batch_size:
                        self.db_manager.commit_capture()
                        self.packet_batch_count = 0
                        
                        # Periodically analyze traffic and update UI
                        self.gui.analyze_traffic()
                        self.gui.update_output(f"Processed {self.packet_count} packets total")
                        self.gui.master.after(0, lambda pc=self.packet_count: self.gui.status_var.set(f"Captured: {pc} packets"))
                
                # Prevent buffer from growing too large (10MB limit)
                if len(buffer) > 10_000_000:
                    self.gui.update_output("Buffer exceeded 10MB limit, resetting...")
                    buffer = ""
            
            # Check for any errors from tshark
            if self.tshark_process:
                errors = self.tshark_process.stderr.read()
                if errors:
                    self.gui.update_output(f"Tshark errors: {errors.decode('utf-8', errors='replace')}")
        
        except PermissionError:
            self.gui.update_output("Permission denied. Run with elevated privileges.")
        except Exception as e:
            self.gui.update_output(f"Capture error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.gui.update_output("Capture stopped")
            self.gui.master.after(0, lambda: self.gui.status_var.set("Ready"))
            if self.tshark_process:
                self.tshark_process.terminate()
                self.tshark_process = None
    
    def determine_packet_type(self, packet_data):
        """Determine packet type for logging purposes"""
        if not packet_data or "layers" not in packet_data:
            return "unknown"
            
        layers = packet_data.get("layers", {})
        
        if "dns_qry_name" in layers:
            return "dns"
        elif "http_host" in layers or "http_request_method" in layers:
            return "http"
        elif "tls_handshake_type" in layers:
            return "tls"
        elif "icmp_type" in layers:
            return "icmp"
        elif "tcp_srcport" in layers:
            return "tcp"
        elif "udp_srcport" in layers:
            return "udp"
        elif "arp_src_proto_ipv4" in layers or "arp_dst_proto_ipv4" in layers:
            return "arp"
        
        return "unknown"
    
    def extract_ek_objects(self, buffer):
        """
        Extract data objects from tshark -T ek output format
        Returns a list of complete JSON data objects (without index lines)
        """
        objects = []
        lines = buffer.strip().split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            if not line:
                i += 1
                continue
            
            # Check if this appears to be an index line (first line of a pair)
            if line.startswith('{"index"'):
                # The next line should be the data line
                if i + 1 < len(lines) and lines[i + 1].strip():
                    data_line = lines[i + 1].strip()
                    try:
                        # Validate it's valid JSON before adding
                        json.loads(data_line)
                        objects.append(data_line)
                        # Move past this pair
                        i += 2
                    except json.JSONDecodeError:
                        # If we can't parse the data line, just move forward one line
                        self.gui.update_output(f"Skipping malformed EK data line: {data_line[:50]}...")
                        i += 1
                else:
                    # Incomplete pair, just move forward
                    i += 1
            else:
                # If this isn't an index line, try parsing it anyway in case it's a data line
                try:
                    json.loads(line)
                    objects.append(line)
                except json.JSONDecodeError:
                    pass
                i += 1
        
        return objects
    
    def get_array_value(self, array_field):
        """
        Extract first value from an array field, which is the typical structure in EK format.
        Returns None if the field is not an array or is empty.
        """
        if isinstance(array_field, list) and array_field:
            return array_field[0]
        return None
    
    def get_layer_value(self, layers, field_name):
        """
        Get a value from the layers object, handling the EK array format.
        Returns the first value from the array if present, otherwise None.
        """
        if field_name in layers:
            return self.get_array_value(layers[field_name])
        return None
    
    def _get_layer_value(self, layers, field_name):
        """
        Get a value from the layers object, handling the array format.
        Returns the first value from the array if present, otherwise None.
        """
        if field_name in layers:
            value = layers[field_name]
            if isinstance(value, list) and value:
                return value[0]
        return None
    
    def process_packet_ek(self, packet_data):
        """Process a packet in Elasticsearch Kibana format using field definitions"""
        try:
            # Get the layers
            layers = packet_data.get("layers", {})
            if not layers:
                return False
            
            # Extract IP addresses (supporting both IPv4 and IPv6) using field definitions
            src_ip = None
            dst_ip = None
            
            # Try IPv4 fields first
            src_ip_field = capture_fields.get_field_by_tshark_name("ip.src")
            dst_ip_field = capture_fields.get_field_by_tshark_name("ip.dst")
            
            if src_ip_field:
                layer_name = src_ip_field["tshark_field"].replace(".", "_")
                src_ip = self.get_layer_value(layers, layer_name)
                
            if dst_ip_field:
                layer_name = dst_ip_field["tshark_field"].replace(".", "_")
                dst_ip = self.get_layer_value(layers, layer_name)
            
            # If not found, try IPv6 fields
            if not src_ip:
                src_ipv6_field = capture_fields.get_field_by_tshark_name("ipv6.src")
                if src_ipv6_field:
                    layer_name = src_ipv6_field["tshark_field"].replace(".", "_")
                    src_ip = self.get_layer_value(layers, layer_name)
                    
            if not dst_ip:
                dst_ipv6_field = capture_fields.get_field_by_tshark_name("ipv6.dst")
                if dst_ipv6_field:
                    layer_name = dst_ipv6_field["tshark_field"].replace(".", "_")
                    dst_ip = self.get_layer_value(layers, layer_name)
            
            # Extract MAC address from ethernet frame if available
            src_mac = None
            eth_src_field = capture_fields.get_field_by_tshark_name("eth.src")
            if eth_src_field:
                layer_name = eth_src_field["tshark_field"].replace(".", "_")
                src_mac = self.get_layer_value(layers, layer_name)
            
            # Check for ARP data - check if any ARP field is present
            if "arp_src_proto_ipv4" in layers or "arp_dst_proto_ipv4" in layers or "arp_opcode" in layers:
                # Process ARP packet using dedicated method - this stores data in capture.db
                self._process_arp_packet_ek(layers)
                # Still continue processing as a normal packet if IP fields are present

            # Basic data validation for IP-based packets
            # For ARP packets, we may not have both IPs, so don't return early
            if (not "arp_src_proto_ipv4" in layers and not "arp_dst_proto_ipv4" in layers) and (not src_ip or not dst_ip):
                # Don't log this too frequently
                if random.random() < 0.05:
                    self.gui.update_output(f"Missing IP addresses in packet - src:{src_ip}, dst:{dst_ip}")
                return False
            
            # Extract port and length information
            src_port, dst_port = self._extract_ports(layers)
            length = self._extract_length(layers)
            
            # Skip processing if the IP is in the false positives list
            if (src_ip and src_ip in self.gui.false_positives) or (dst_ip and dst_ip in self.gui.false_positives):
                return False

            # Create a connection key that includes ports if available
            if src_ip and dst_ip:
                if src_port is not None and dst_port is not None:
                    connection_key = f"{src_ip}:{src_port}->{dst_ip}:{dst_port}"
                else:
                    connection_key = f"{src_ip}->{dst_ip}"
                
                # Extract TTL value and add it to connections
                ttl_value = self.get_layer_value(layers, "ip_ttl")
                if ttl_value is None:
                    # Try IPv6 hop limit if IPv4 TTL not found
                    ttl_value = self.get_layer_value(layers, "ipv6_hlim")
                    
                if ttl_value is not None:
                    try:
                        ttl = int(ttl_value)
                        # Update connection with TTL
                        self.db_manager.update_connection_ttl(connection_key, ttl)
                    except ValueError:
                        pass

                # Check for RDP connection (port 3389)
                is_rdp = 0
                if dst_port == 3389:
                    is_rdp = 1
                    self.gui.update_output(f"Detected RDP connection from {src_ip}:{src_port} to {dst_ip}:{dst_port}")
                
                # Process protocol-specific data to store in capture.db
                # This doesn't do advanced analysis but ensures data is available for analysis_manager
                
                # Check for DNS protocol
                if "dns_qry_name" in layers:
                    self._store_dns_data(layers, src_ip)
                
                # Check for HTTP protocol
                if self._has_http_data(layers):
                    self._store_http_data(layers, src_ip, dst_ip, src_port, dst_port, connection_key)
                
                # Check for TLS protocol
                if self._has_tls_data(layers):
                    self._store_tls_data(layers, connection_key)
                
                # Check for ICMP protocol
                if "icmp_type" in layers:
                    self._store_icmp_data(layers, src_ip, dst_ip)
                
                # Check for SMB data
                if "smb_filename" in layers or "smb_session_setup_account" in layers:
                    smb_filename = self.get_layer_value(layers, "smb_filename")
                    if smb_filename:
                        self._store_smb_data(layers, src_ip, dst_ip, src_port, dst_port, connection_key)
                    # Store SMB authentication data
                    self._store_smb_auth(layers, connection_key)
                
                # Track ports for port scan detection
                if dst_port and self.analysis_manager:
                    self.analysis_manager.add_port_scan_data(src_ip, dst_ip, dst_port)
                    
                # Store basic protocol info based on port
                if dst_port and self.analysis_manager:
                    # Store based on common port numbers - simplified detection
                    if dst_port == 80:
                        self.analysis_manager.add_app_protocol(connection_key, "HTTP", detection_method="port-based")
                    elif dst_port == 443:
                        self.analysis_manager.add_app_protocol(connection_key, "HTTPS", detection_method="port-based")
                    elif dst_port == 53 or src_port == 53:
                        self.analysis_manager.add_app_protocol(connection_key, "DNS", detection_method="port-based")
                    elif dst_port == 445 or src_port == 445:
                        self.analysis_manager.add_app_protocol(connection_key, "SMB", detection_method="port-based")
                    elif dst_port == 88:
                        self.analysis_manager.add_app_protocol(connection_key, "Kerberos", detection_method="port-based")
                    elif dst_port == 3389:
                        self.analysis_manager.add_app_protocol(connection_key, "RDP", detection_method="port-based")
                        
                # Store authentication data if present
                if "http_authorization" in layers:
                    self._store_http_auth(layers, connection_key)
                    
                if "ntlmssp_negotiateflags" in layers or "ntlmssp_ntlmserverchallenge" in layers or "ntlmssp_ntlmv2_response" in layers:
                    self._store_ntlm_auth(layers, connection_key)
                    
                if "http_file_data" in layers:
                    self._store_http_file_data(layers, connection_key)
                    
                if "http_cookie" in layers:
                    self._store_http_cookies(layers, connection_key)
                    
                if "kerberos_CNameString" in layers or "kerberos_realm" in layers or "kerberos_msg_type" in layers:
                    self._store_kerberos_auth(layers, connection_key)
                
                # Store the basic connection in the database with MAC address
                if src_ip and dst_ip:  # Only add if we have both IPs
                    return self.db_manager.add_packet(
                        connection_key, src_ip, dst_ip, src_port, dst_port, length, is_rdp, src_mac
                    )
                    
            return True  # Return True for successful processing
                    
        except Exception as e:
            self.gui.update_output(f"Error processing packet: {e}")
            import traceback
            traceback.print_exc()
            return False

    
    def _has_http_data(self, layers):
        """Check if layers contain HTTP data"""
        return any(key in layers for key in ["http_host", "http_request_method", "http_request_uri", "http_response_code"])
    
    def _has_tls_data(self, layers):
        """Check if layers contain TLS data"""
        return any(key in layers for key in ["tls_handshake_type", "tls_handshake_version"])
    
    def _extract_ports(self, layers):
        """Extract source and destination ports from packet layers"""
        src_port = None
        dst_port = None
        
        # Try TCP ports first
        tcp_src_field = capture_fields.get_field_by_tshark_name("tcp.srcport")
        if tcp_src_field:
            layer_name = tcp_src_field["tshark_field"].replace(".", "_")
            tcp_src = self.get_layer_value(layers, layer_name)
            if tcp_src:
                try:
                    src_port = int(tcp_src)
                except (ValueError, TypeError):
                    pass
        
        tcp_dst_field = capture_fields.get_field_by_tshark_name("tcp.dstport")
        if tcp_dst_field:
            layer_name = tcp_dst_field["tshark_field"].replace(".", "_")
            tcp_dst = self.get_layer_value(layers, layer_name)
            if tcp_dst:
                try:
                    dst_port = int(tcp_dst)
                except (ValueError, TypeError):
                    pass
        
        # If not found, try UDP ports
        if src_port is None:
            udp_src_field = capture_fields.get_field_by_tshark_name("udp.srcport")
            if udp_src_field:
                layer_name = udp_src_field["tshark_field"].replace(".", "_")
                udp_src = self.get_layer_value(layers, layer_name)
                if udp_src:
                    try:
                        src_port = int(udp_src)
                    except (ValueError, TypeError):
                        pass
        
        if dst_port is None:
            udp_dst_field = capture_fields.get_field_by_tshark_name("udp.dstport")
            if udp_dst_field:
                layer_name = udp_dst_field["tshark_field"].replace(".", "_")
                udp_dst = self.get_layer_value(layers, layer_name)
                if udp_dst:
                    try:
                        dst_port = int(udp_dst)
                    except (ValueError, TypeError):
                        pass
                        
        return src_port, dst_port
    
    def _extract_length(self, layers):
        """Extract frame length from packet layers"""
        length = 0
        frame_len_field = capture_fields.get_field_by_tshark_name("frame.len")
        if frame_len_field:
            layer_name = frame_len_field["tshark_field"].replace(".", "_")
            frame_len = self.get_layer_value(layers, layer_name)
            if frame_len:
                try:
                    length = int(frame_len)
                except (ValueError, TypeError):
                    pass
        return length

    def _store_dns_data(self, layers, src_ip):
        """Extract all DNS data and store to capture.db with additional fields"""
        try:
            # Extract query name and type
            query_name = self.get_layer_value(layers, "dns_qry_name")
            if not query_name:
                return False
                
            query_type = self.get_layer_value(layers, "dns_qry_type") or "unknown"
            
            # Also extract response data if available
            resp_name = self.get_layer_value(layers, "dns_resp_name")
            resp_type = self.get_layer_value(layers, "dns_resp_type")
            
            # Extract new fields
            ttl = self.get_layer_value(layers, "dns_ttl")
            cname = self.get_layer_value(layers, "dns_cname")
            ns = self.get_layer_value(layers, "dns_ns")
            a_record = self.get_layer_value(layers, "dns_a")
            aaaa_record = self.get_layer_value(layers, "dns_aaaa")
            
            # Convert TTL to integer if present
            ttl_int = None
            if ttl:
                try:
                    ttl_int = int(ttl)
                except (ValueError, TypeError):
                    ttl_int = None
            
            # Store DNS query in database with all fields
            return self.db_manager.add_dns_query(
                src_ip, 
                query_name, 
                query_type,
                resp_name,
                resp_type,
                ttl_int,
                cname,
                ns,
                a_record,
                aaaa_record
            )
        except Exception as e:
            self.gui.update_output(f"Error storing DNS data: {e}")
            return False

    def _store_http_data(self, layers, src_ip, dst_ip, src_port, dst_port, connection_key):
        """Extract HTTP data and store to capture.db including individual headers"""
        try:
            # Extract HTTP fields (including new ones)
            method = self.get_layer_value(layers, "http_request_method")
            uri = self.get_layer_value(layers, "http_request_uri")
            host = self.get_layer_value(layers, "http_host")
            user_agent = self.get_layer_value(layers, "http_user_agent")
            referer = self.get_layer_value(layers, "http_referer")  # New field
            x_forwarded_for = self.get_layer_value(layers, "http_x_forwarded_for")  # New field
            status_code_raw = self.get_layer_value(layers, "http_response_code")
            server = self.get_layer_value(layers, "http_server")
            content_type = self.get_layer_value(layers, "http_content_type")
            content_length_raw = self.get_layer_value(layers, "http_content_length")
            
            # Parse content length if present
            content_length = 0
            if content_length_raw:
                try:
                    content_length = int(content_length_raw)
                except (ValueError, TypeError):
                    content_length = 0
            
            # Determine if this is a request or response
            is_request = method is not None or uri is not None or host is not None
            
            # Store HTTP request if present
            request_id = None
            if is_request:
                # Create headers dictionary with new fields
                headers = {"Host": host or dst_ip}
                if user_agent:
                    headers["User-Agent"] = user_agent
                if referer:
                    headers["Referer"] = referer
                if x_forwarded_for:
                    headers["X-Forwarded-For"] = x_forwarded_for
                if content_type:
                    headers["Content-Type"] = content_type
                if content_length > 0:
                    headers["Content-Length"] = str(content_length)
                    
                # Convert headers to JSON
                headers_json = json.dumps(headers)
                
                # Store HTTP request with new fields
                request_id = self.db_manager.add_http_request(
                    connection_key,
                    method or "GET",  # Default method
                    host or dst_ip,   # Use destination IP if host not available
                    uri or "/",       # Default URI
                    "HTTP/1.1",       # Assumed version
                    user_agent or "",
                    referer or "",    # New field
                    content_type or "",
                    headers_json,
                    content_length,
                    x_forwarded_for or ""  # New field
                )
                
                # Store individual headers in http_headers table
                if request_id:
                    self.db_manager.add_http_headers(request_id, connection_key, headers_json, is_request=True)
            
            # Store HTTP response if present
            if status_code_raw and request_id:
                try:
                    status_code = int(status_code_raw)
                    
                    # Create headers dictionary
                    headers = {}
                    if server:
                        headers["Server"] = server
                    if content_type:
                        headers["Content-Type"] = content_type
                    if content_length > 0:
                        headers["Content-Length"] = str(content_length)
                        
                    # Convert headers to JSON
                    headers_json = json.dumps(headers)
                    
                    # Store HTTP response
                    self.db_manager.add_http_response(
                        request_id,
                        status_code,
                        content_type or "",
                        content_length,
                        server or "",
                        headers_json
                    )
                    
                    # Store individual headers in http_headers table
                    self.db_manager.add_http_headers(request_id, connection_key, headers_json, is_request=False)
                except (ValueError, TypeError):
                    pass
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing HTTP data: {e}")
            return False
        
    def _store_smb_data(self, layers, src_ip, dst_ip, src_port, dst_port, connection_key):
        """Store SMB file access information"""
        try:
            filename = self.get_layer_value(layers, "smb_filename")
            if not filename:
                return False
            
            # Store SMB file access in database
            current_time = time.time()
            operation = "access"  # Default operation
            size = 0  # Default size
            
            return self.db_manager.add_smb_file(
                connection_key,
                filename,
                operation,
                size,
                current_time
            )
        except Exception as e:
            self.gui.update_output(f"Error storing SMB data: {e}")
            return False
        
    def _detect_application_protocol(self, src_ip, dst_ip, src_port, dst_port, layers, connection_key):
        """Detect application protocol based on port numbers and packet content"""
        try:
            # Common protocol port mappings
            tcp_port_protocols = {
                21: "FTP",
                22: "SSH",
                23: "Telnet",
                25: "SMTP",
                80: "HTTP",
                110: "POP3",
                119: "NNTP",
                143: "IMAP",
                443: "HTTPS",
                465: "SMTPS",
                993: "IMAPS",
                995: "POP3S",
                1433: "MSSQL",
                1521: "Oracle",
                3306: "MySQL",
                3389: "RDP",
                5432: "PostgreSQL",
                5900: "VNC",
                6379: "Redis",
                8080: "HTTP-ALT",
                8443: "HTTPS-ALT",
                9418: "Git",
                27017: "MongoDB"
            }
            
            udp_port_protocols = {
                53: "DNS",
                67: "DHCP",
                69: "TFTP",
                123: "NTP",
                161: "SNMP",
                500: "IPsec",
                514: "Syslog",
                1900: "SSDP",
                5353: "mDNS"
            }
            
            protocol = None
            
            # Check if this is TCP (by checking for TCP fields)
            is_tcp = "tcp_srcport" in layers
            port_map = tcp_port_protocols if is_tcp else udp_port_protocols
            
            # Check destination port
            if dst_port in port_map:
                protocol = port_map[dst_port]
            # Check source port (less reliable but still useful)
            elif src_port in port_map:
                protocol = port_map[src_port]
            
            # If we detected a protocol, store it
            if protocol:
                return self.db_manager.add_app_protocol(
                    connection_key, protocol, detection_method="port-based"
                )
            
            return False
        except Exception as e:
            self.gui.update_output(f"Error detecting application protocol: {e}")
            return False

    def _store_tls_data(self, layers, connection_key):
        """Extract TLS data and store to capture.db with new fields"""
        try:
            # Extract TLS fields including new ones
            tls_version = self.get_layer_value(layers, "tls_handshake_version")
            cipher_suite = self.get_layer_value(layers, "tls_handshake_ciphersuite")
            server_name = self.get_layer_value(layers, "tls_handshake_extensions_server_name")
            record_content_type = self.get_layer_value(layers, "tls_record_content_type")  # New field
            session_id = self.get_layer_value(layers, "ssl_handshake_session_id")  # New field
            
            # Convert record_content_type to integer if present
            content_type_int = None
            if record_content_type:
                try:
                    content_type_int = int(record_content_type)
                except (ValueError, TypeError):
                    content_type_int = None
            
            # Set default values if needed
            if not tls_version:
                # Try to determine from connection key
                if "443" in connection_key:
                    tls_version = "TLSv1.2 (assumed)"
                else:
                    tls_version = "Unknown"
            
            if not cipher_suite:
                cipher_suite = "Unknown"
            
            # Extract destination IP from connection key
            dst_ip = connection_key.split('->')[1].split(':')[0] if '->' in connection_key else None
            
            # If no server name available, use destination IP
            if not server_name and dst_ip:
                server_name = dst_ip
            
            # Store in database with new fields
            ja3_fingerprint = ""
            ja3s_fingerprint = ""
            cert_issuer = ""
            cert_subject = ""
            cert_valid_from = ""
            cert_valid_to = ""
            cert_serial = ""
            
            # Store TLS connection info
            return self.db_manager.add_tls_connection(
                connection_key, 
                tls_version, 
                cipher_suite, 
                server_name, 
                ja3_fingerprint, 
                ja3s_fingerprint, 
                cert_issuer, 
                cert_subject,
                cert_valid_from, 
                cert_valid_to, 
                cert_serial,
                content_type_int,  # New field
                session_id         # New field
            )
        except Exception as e:
            self.gui.update_output(f"Error storing TLS data: {e}")
            return False
    
    def _store_icmp_data(self, layers, src_ip, dst_ip):
        """Extract basic ICMP data and store to capture.db"""
        try:
            # Extract ICMP type
            icmp_type_raw = self.get_layer_value(layers, "icmp_type")
            icmp_type = 0
            
            if icmp_type_raw is not None:
                try:
                    icmp_type = int(icmp_type_raw)
                except (ValueError, TypeError):
                    icmp_type = 0
            
            # Store ICMP packet
            return self.db_manager.add_icmp_packet(src_ip, dst_ip, icmp_type)
        except Exception as e:
            self.gui.update_output(f"Error storing ICMP data: {e}")
            return False
        
    def _store_http_auth(self, layers, connection_key):
        """Store HTTP authentication data"""
        try:
            # Get authorization header
            auth_header = self._get_layer_value(layers, "http_authorization")
            if not auth_header:
                return False
                
            # Determine auth type (Basic, NTLM, Bearer, etc.)
            auth_type = "Unknown"
            username = ""
            credentials = ""
            
            if auth_header.startswith("Basic "):
                auth_type = "Basic"
                # Extract credentials
                try:
                    encoded_creds = auth_header.split(' ')[1]
                    decoded = base64.b64decode(encoded_creds).decode('utf-8', errors='ignore')
                    if ':' in decoded:
                        username, password = decoded.split(':', 1)
                        credentials = f"{username}:{password}"
                except:
                    pass
            elif auth_header.startswith("NTLM "):
                auth_type = "NTLM"
            elif auth_header.startswith("Bearer "):
                auth_type = "Bearer" 
            elif auth_header.startswith("Digest "):
                auth_type = "Digest"
                
            current_time = time.time()
            
            # Store in database
            cursor = self.db_manager.capture_conn.cursor()
            cursor.execute("""
                INSERT INTO http_auth
                (connection_key, auth_type, auth_header, username, credentials, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (connection_key, auth_type, auth_header, username, credentials, current_time))
            self.db_manager.capture_conn.commit()
            cursor.close()
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing HTTP auth: {e}")
            return False

    def _store_ntlm_auth(self, layers, connection_key):
        """Store NTLM authentication data"""
        try:
            # Check if this is an NTLM authentication packet
            negotiate_flags = self.get_layer_value(layers, "ntlmssp_negotiateflags")  # Updated
            ntlm_challenge = self.get_layer_value(layers, "ntlmssp_ntlmserverchallenge")  # Updated
            ntlmv2_response = self.get_layer_value(layers, "ntlmssp_ntlmv2_response")
            
            # Skip if none of the NTLM fields are present
            if not negotiate_flags and not ntlm_challenge and not ntlmv2_response:
                return False
                
            # Extract domain and username if available
            domain = self.get_layer_value(layers, "ntlmssp_domain_name")  # Updated
            username = self.get_layer_value(layers, "ntlmssp_auth_username")  # Updated
                
            current_time = time.time()
            
            # Store in database
            cursor = self.db_manager.capture_conn.cursor()
            cursor.execute("""
                INSERT INTO ntlm_auth
                (connection_key, negotiate_flags, ntlm_challenge, ntlmv2_response, domain, username, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (connection_key, negotiate_flags, ntlm_challenge, ntlmv2_response, domain, username, current_time))
            self.db_manager.capture_conn.commit()
            cursor.close()
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing NTLM auth: {e}")
            return False

    def _store_http_file_data(self, layers, connection_key):
        """Store HTTP file data content"""
        try:
            # Get file data
            file_data = self._get_layer_value(layers, "http_file_data")
            if not file_data:
                return False
                
            # Determine content type if available
            content_type = self._get_layer_value(layers, "http_content_type")
                
            current_time = time.time()
            
            # Store in database
            cursor = self.db_manager.capture_conn.cursor()
            cursor.execute("""
                INSERT INTO http_file_data
                (connection_key, content_type, file_data, timestamp)
                VALUES (?, ?, ?, ?)
            """, (connection_key, content_type, file_data, current_time))
            self.db_manager.capture_conn.commit()
            cursor.close()
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing HTTP file data: {e}")
            return False

    def _store_http_cookies(self, layers, connection_key):
        """Store HTTP cookie data"""
        try:
            # Get cookie data
            cookie_value = self._get_layer_value(layers, "http_cookie")
            if not cookie_value:
                return False
                
            # Try to get domain
            host = self._get_layer_value(layers, "http_host")
                
            current_time = time.time()
            
            # Store in database
            cursor = self.db_manager.capture_conn.cursor()
            cursor.execute("""
                INSERT INTO http_cookies
                (connection_key, cookie_name, cookie_value, domain, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (connection_key, "", cookie_value, host, current_time))
            self.db_manager.capture_conn.commit()
            cursor.close()
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing HTTP cookies: {e}")
            return False

    def _store_kerberos_auth(self, layers, connection_key):
        """Store Kerberos authentication data"""
        try:
            # Get Kerberos fields
            username = self._get_layer_value(layers, "kerberos_CNameString")
            realm = self._get_layer_value(layers, "kerberos_realm")
            msg_type = self._get_layer_value(layers, "kerberos_msg_type")
            
            # Skip if no Kerberos data
            if not username and not realm and not msg_type:
                return False
                
            current_time = time.time()
            
            # Convert msg_type to integer if present
            msg_type_int = None
            if msg_type:
                try:
                    msg_type_int = int(msg_type)
                except:
                    pass
            
            # Store in database
            cursor = self.db_manager.capture_conn.cursor()
            cursor.execute("""
                INSERT INTO kerberos_auth
                (connection_key, msg_type, username, realm, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (connection_key, msg_type_int, username, realm, current_time))
            self.db_manager.capture_conn.commit()
            cursor.close()
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing Kerberos auth: {e}")
            return False

    def _store_smb_auth(self, layers, connection_key):
        """Store SMB authentication data"""
        try:
            # Get SMB auth fields
            account = self.get_layer_value(layers, "ntlmssp_auth_username")  # Updated
            domain = self.get_layer_value(layers, "ntlmssp_domain_name")  # Updated
            
            # Skip if no SMB auth data
            if not account and not domain:
                return False
                
            current_time = time.time()
            
            # Store in database
            cursor = self.db_manager.capture_conn.cursor()
            cursor.execute("""
                INSERT INTO smb_auth
                (connection_key, domain, account, timestamp)
                VALUES (?, ?, ?, ?)
            """, (connection_key, domain, account, current_time))
            self.db_manager.capture_conn.commit()
            cursor.close()
            
            return True
        except Exception as e:
            self.gui.update_output(f"Error storing SMB auth: {e}")
            return False

    def _process_arp_packet_ek(self, layers):
        """Extract and store ARP packet information with MAC address"""
        try:
            # Extract ARP source and destination IPs
            arp_src_ip = self.get_layer_value(layers, "arp_src_proto_ipv4")
            arp_dst_ip = self.get_layer_value(layers, "arp_dst_proto_ipv4")
            
            # Extract ARP operation (1=request, 2=reply)
            operation_raw = self.get_layer_value(layers, "arp_opcode")
            operation = 0
            if operation_raw:
                try:
                    operation = int(operation_raw)
                except (ValueError, TypeError):
                    operation = 0
            
            # Extract MAC address (new field)
            src_mac = self.get_layer_value(layers, "arp_src_hw_mac")
            
            # At least one IP should be present for ARP
            if arp_src_ip or arp_dst_ip:
                # Store ARP data with MAC address
                current_time = time.time()
                success = self.db_manager.add_arp_data(
                    arp_src_ip or "Unknown",  # Use "Unknown" if source IP is not available
                    arp_dst_ip or "Unknown",  # Use "Unknown" if destination IP is not available
                    operation,
                    current_time,
                    src_mac  # New field
                )
                
                # Occasionally log ARP packets for debugging
                if success and random.random() < 0.1:  # Log ~10% of ARP packets
                    op_type = "request" if operation == 1 else "reply" if operation == 2 else f"unknown({operation})"
                    if src_mac:
                        self.gui.update_output(f"ARP {op_type}: {arp_src_ip or 'Unknown'} ({src_mac}) -> {arp_dst_ip or 'Unknown'}")
                    else:
                        self.gui.update_output(f"ARP {op_type}: {arp_src_ip or 'Unknown'} -> {arp_dst_ip or 'Unknown'}")
                        
                return success
            
            return False
        except Exception as e:
            self.gui.update_output(f"Error processing ARP packet: {e}")
            import traceback
            traceback.print_exc()
            return False

    def add_alert(self, ip_address, alert_message, rule_name):
        """Add an alert through the analysis manager"""
        # Check if we have an analysis manager
        if self.analysis_manager:
            return self.analysis_manager.add_alert(ip_address, alert_message, rule_name)
        
        # Fallback to old method
        if alert_message not in self.alerts_by_ip[ip_address]:
            self.alerts_by_ip[ip_address].add(alert_message)
            
            # Queue the alert for processing
            return self.db_manager.queue_alert(ip_address, alert_message, rule_name)
        return False
    
