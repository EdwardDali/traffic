# SubtabBase class is injected by the Loader
import tkinter as tk
from tkinter import ttk, messagebox
import os
import geoip2.database
import geoip2.errors
import threading
import time
import ipaddress
import urllib.request
import io
from datetime import datetime
from PIL import Image, ImageTk

class GeoLocationSubtab(SubtabBase):
    """Subtab that displays alerts with geographical information"""
    
    def __init__(self):
        super().__init__(
            name="Geo Location",
            description="Displays alerts with geographical information"
        )
        self.geo_tree = None
        self.geo_details_tree = None
        self.selected_ip_var = None
        self.geo_data_cache = {}
        self.city_reader = None
        self.country_reader = None
        self.city_db_path = None
        self.country_db_path = None
        self.ip_filter = None
        self.map_image = None
        self.map_photo = None
        
    def create_ui(self):
        """Create UI components for Geo Location subtab"""
        # Control buttons frame
        control_frame = ttk.Frame(self.tab_frame)
        control_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Button(control_frame, text="Refresh Geo Data", 
                   command=self.refresh).pack(side="left", padx=5)
        
        ttk.Button(control_frame, text="Show World Map", 
                   command=self.show_map).pack(side="left", padx=5)
        
        # Database path section - now just shows status since paths are fixed
        self.db_status_var = tk.StringVar(value="Database status: Not loaded")
        ttk.Label(control_frame, textvariable=self.db_status_var).pack(side="left", padx=5)
        
        ttk.Button(control_frame, text="Load Databases", 
                   command=self.load_geoip_databases).pack(side="left", padx=5)
                   
        # IP filter frame - add this to allow filtering by IP address
        filter_frame = ttk.Frame(self.tab_frame)
        filter_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(filter_frame, text="Filter by IP:").pack(side="left", padx=5)
        self.ip_filter = ttk.Entry(filter_frame, width=20)
        self.ip_filter.pack(side="left", padx=5)
        
        ttk.Button(filter_frame, text="Apply Filter", 
                  command=lambda: self.refresh(self.ip_filter.get())).pack(side="left", padx=5)
        ttk.Button(filter_frame, text="Clear Filter", 
                  command=lambda: (self.ip_filter.delete(0, tk.END), self.refresh())).pack(side="left", padx=5)
                  
        # Add a button to filter only for external IPs
        self.show_only_external_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_frame, text="Show Only External IPs", 
                      variable=self.show_only_external_var,
                      command=self.refresh).pack(side="right", padx=5)
        
        # Geo location treeview
        self.geo_tree, _ = gui.tab_factory.create_tree_with_scrollbar(
            self.tab_frame,
            columns=("ip", "country", "city", "alerts"),
            headings=["IP Address", "Country", "City", "Alert Count"],
            widths=[150, 100, 150, 80],
            height=10
        )
        
        # Create details frame
        details_frame = ttk.LabelFrame(self.tab_frame, text="Location Details")
        details_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.geo_details_tree, _ = gui.tab_factory.create_tree_with_scrollbar(
            details_frame,
            columns=("detail", "value"),
            headings=["Detail", "Value"],
            widths=[150, 300],
            height=10
        )
        
        # Selected IP frame
        info_frame = ttk.Frame(self.tab_frame)
        info_frame.pack(fill="x", padx=10, pady=5)
        
        self.selected_ip_var = tk.StringVar()
        ttk.Label(info_frame, text="Selected IP:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(info_frame, textvariable=self.selected_ip_var, width=30).grid(row=0, column=1, sticky="w", padx=5, pady=5)
        
        button_frame = ttk.Frame(info_frame)
        button_frame.grid(row=0, column=2, sticky="e", padx=5, pady=5)
        
        ttk.Button(button_frame, text="Copy IP", 
                  command=lambda: gui.ip_manager.copy_ip_to_clipboard(self.selected_ip_var.get())
                 ).pack(side="left", padx=5)
        
        ttk.Button(button_frame, text="Mark as False Positive", 
                  command=lambda: gui.ip_manager.mark_as_false_positive(self.selected_ip_var.get())
                 ).pack(side="left", padx=5)
        
        # Make the third column (with buttons) expand
        info_frame.columnconfigure(2, weight=1)
        
        # Bind event to show details for selected IP
        self.geo_tree.bind("<<TreeviewSelect>>", self.show_location_details)
        
        # Create context menu
        gui.ip_manager.create_context_menu(
            self.geo_tree, 
            self.selected_ip_var, 
            lambda: self.show_location_details(None)
        )
        
        # Try to load the GeoIP databases
        self.load_geoip_databases()
        
        # Try to download the world map
        self.download_world_map()
    
    def download_world_map(self):
        """Download or load a world map image for the visualization"""
        # First check if a local map exists in the utils folder
        utils_path = os.path.join(gui.app_root, "utils")
        local_map_path = os.path.join(utils_path, "world_map.jpg")
        
        # Try to load local map first
        if os.path.exists(local_map_path):
            try:
                self.map_image = Image.open(local_map_path)
                self.update_output("Loaded world map from local file")
                return
            except Exception as e:
                self.update_output(f"Error loading local map: {e}")
        
        # If local map loading failed or the file doesn't exist, try downloading
        try:
            # Use a simple world map from OpenStreetMap or a similar source
            map_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/8/83/Equirectangular_projection_SW.jpg/1280px-Equirectangular_projection_SW.jpg"
            
            # Ask user if they want to download
            if messagebox.askyesno("Download Map", 
                                "No local map file found. Would you like to download a world map image?\n\n"
                                "The map will be used for IP visualization."):
                # Show downloading status
                self.update_output("Downloading world map...")
                
                # Download the image in a background thread to avoid freezing the UI
                threading.Thread(
                    target=self._background_map_download,
                    args=(map_url, local_map_path),  # Pass the local path to save the downloaded file
                    daemon=True
                ).start()
            else:
                self.update_output("Map download cancelled. Using simple map visualization.")
                
        except Exception as e:
            self.update_output(f"Error with map download dialog: {e}")

    
    def _background_map_download(self, url, save_path):
        """Download the world map image in a background thread and save it locally"""
        try:
            # Download the image
            with urllib.request.urlopen(url) as response:
                image_data = response.read()
            
            # Open the image with PIL
            self.map_image = Image.open(io.BytesIO(image_data))
            
            # Save the image for future use
            try:
                # Create directory if needed
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                # Save the image
                self.map_image.save(save_path)
                self.update_output(f"World map downloaded and saved to {save_path}")
            except Exception as e:
                self.update_output(f"Error saving downloaded map: {e}")
            
            # Update status
            self.update_output("World map downloaded successfully")
        except Exception as e:
            self.update_output(f"Error in map download: {e}")
    
    def is_private_ip(self, ip_str):
        """Check if an IP address is private/local"""
        try:
            ip = ipaddress.ip_address(ip_str)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            # If the IP is invalid, just return False
            return False
    
    def load_geoip_databases(self):
        """Load both city and country GeoIP databases"""
        # Define paths in utils folder
        utils_path = os.path.join(gui.app_root, "utils")
        city_db_path = os.path.join(utils_path, "GeoLite2-City.mmdb")
        country_db_path = os.path.join(utils_path, "GeoLite2-Country.mmdb")
        
        # Check if files exist
        city_exists = os.path.exists(city_db_path)
        country_exists = os.path.exists(country_db_path)
        
        # Close existing readers if they exist
        if self.city_reader:
            self.city_reader.close()
            self.city_reader = None
            
        if self.country_reader:
            self.country_reader.close()
            self.country_reader = None
        
        # Load databases that exist
        status_messages = []
        
        # Try to load City database
        if city_exists:
            try:
                self.city_reader = geoip2.database.Reader(city_db_path)
                self.city_db_path = city_db_path
                status_messages.append("City DB: Loaded")
            except Exception as e:
                self.update_output(f"Error loading City database: {e}")
                status_messages.append("City DB: Error")
        else:
            status_messages.append("City DB: Not found")
        
        # Try to load Country database
        if country_exists:
            try:
                self.country_reader = geoip2.database.Reader(country_db_path)
                self.country_db_path = country_db_path
                status_messages.append("Country DB: Loaded")
            except Exception as e:
                self.update_output(f"Error loading Country database: {e}")
                status_messages.append("Country DB: Error")
        else:
            status_messages.append("Country DB: Not found")
        
        # Update status
        self.db_status_var.set(" | ".join(status_messages))
        
        # Show warning if neither database was loaded
        if not self.city_reader and not self.country_reader:
            self.update_output("No GeoIP databases could be loaded")
            messagebox.showwarning("Databases Not Found", 
                                 f"GeoIP databases not found in {utils_path}\n\n"
                                 "Please download GeoLite2-City.mmdb and GeoLite2-Country.mmdb from MaxMind "
                                 "and place them in the utils/ folder.")
            return False
        
        # Success message
        if self.city_reader or self.country_reader:
            self.update_output("GeoIP databases loaded successfully")
            self.refresh()  # Refresh the display
            return True
            
        return False
    
    def get_ip_location(self, ip):
        """Get location data for an IP address using geoip2 with fallback"""
        if ip in self.geo_data_cache:
            return self.geo_data_cache[ip]
        
        # Check if it's a private/local IP first
        if self.is_private_ip(ip):
            location = {
                "country": "Local Network",
                "city": "Private IP Range",
                "region": "Internal Network",
                "postal": "N/A",
                "latitude": 0,
                "longitude": 0,
                "isp": "Local Network",
                "organization": "Local Network",
                "timezone": "Local",
                "database": "Local",
                "is_local": True
            }
            # Cache the result
            self.geo_data_cache[ip] = location
            return location
        
        location = {"country": "Unknown", "city": "Unknown"}
        
        # Try city database first for more detailed info
        if self.city_reader:
            try:
                city_response = self.city_reader.city(ip)
                
                # Extract location information
                location = {
                    "country": city_response.country.name or "Unknown",
                    "city": city_response.city.name or "Unknown",
                    "region": city_response.subdivisions.most_specific.name if city_response.subdivisions else "Unknown",
                    "postal": city_response.postal.code or "Unknown",
                    "latitude": city_response.location.latitude or 0,
                    "longitude": city_response.location.longitude or 0,
                    "isp": "Unknown",  # Not available in GeoLite2
                    "organization": "Unknown",  # Not available in GeoLite2
                    "timezone": city_response.location.time_zone or "Unknown",
                    "database": "City",
                    "is_local": False
                }
                
                # Cache the result
                self.geo_data_cache[ip] = location
                return location
                
            except geoip2.errors.AddressNotFoundError:
                # IP not found in City database, try Country database next
                pass
            except Exception as e:
                self.update_output(f"Error looking up IP {ip} in City database: {e}")
                # Fall through to country database
        
        # Try country database as fallback
        if self.country_reader:
            try:
                country_response = self.country_reader.country(ip)
                
                # Build minimal location info
                location = {
                    "country": country_response.country.name or "Unknown",
                    "city": "Unknown",  # Not available in Country database
                    "region": "Unknown",  # Not available in Country database
                    "postal": "Unknown",  # Not available in Country database
                    "latitude": 0,  # Not available in Country database
                    "longitude": 0,  # Not available in Country database
                    "isp": "Unknown",
                    "organization": "Unknown",
                    "timezone": "Unknown",
                    "database": "Country",
                    "is_local": False
                }
                
                # Cache the result
                self.geo_data_cache[ip] = location
                return location
                
            except geoip2.errors.AddressNotFoundError:
                # IP not found in either database
                location = {"country": "Not Found", "city": "Not Found", "database": "None", "is_local": False}
                self.geo_data_cache[ip] = location
                return location
            except Exception as e:
                self.update_output(f"Error looking up IP {ip} in Country database: {e}")
                location = {"country": "Error", "city": "Error", "database": "Error", "is_local": False}
                self.geo_data_cache[ip] = location
                return location
        
        # Neither database is available or lookup failed in both
        location = {"country": "No Database", "city": "No Database", "database": "None", "is_local": False}
        self.geo_data_cache[ip] = location
        return location
    
    def refresh(self, ip_filter=None):
        """Refresh geo location data with actual database queries"""
        # Clear existing items
        gui.tree_manager.clear_tree(self.geo_tree)
        
        if not self.city_reader and not self.country_reader:
            self.update_output("No GeoIP databases loaded. Please load databases first.")
            return
            
        # Create a new function to get alert IPs from the database
        def get_alert_ips():
            try:
                cursor = gui.db_manager.analysis_conn.cursor()
                
                # Base query to get IPs and alert counts
                sql = """
                    SELECT ip_address, COUNT(*) as alert_count, MAX(timestamp) as last_seen
                    FROM alerts
                """
                
                # Add filter if provided
                params = ()
                if ip_filter:
                    sql += " WHERE ip_address LIKE ?"
                    params = (f"%{ip_filter}%",)
                
                # Group and order results
                sql += " GROUP BY ip_address ORDER BY alert_count DESC LIMIT 100"
                
                rows = cursor.execute(sql, params).fetchall()
                return rows
            except Exception as e:
                self.update_output(f"Error querying database: {e}")
                return []
        
        # Queue the database query
        gui.db_manager.queue_query(
            get_alert_ips,
            callback=self._process_geo_data
        )
        
        self.update_output("Geo location refresh queued")
    
    def _process_geo_data(self, rows):
        """Process the database results and update the display with geolocation data"""
        if not rows:
            self.update_output("No alert data found")
            return
            
        # Start a background thread to process geolocation lookups
        # This prevents the UI from freezing during lookups
        threading.Thread(
            target=self._background_geo_processing,
            args=(rows,),
            daemon=True
        ).start()
    
    def _background_geo_processing(self, rows):
        """Process geolocation data in a background thread"""
        geo_data = []
        
        # Process each IP
        for i, (ip, alert_count, timestamp) in enumerate(rows):
            # Get location data
            location = self.get_ip_location(ip)
            
            # Skip local IPs if filter is enabled
            if self.show_only_external_var.get() and location.get("is_local", False):
                continue
            
            # Add to result list
            geo_data.append((
                ip,
                location.get("country", "Unknown"),
                location.get("city", "Unknown"),
                alert_count
            ))
            
            # Update progress periodically
            if i % 10 == 0:
                self.update_output(f"Processed {i+1}/{len(rows)} IP addresses")
        
        # Update UI in the main thread
        gui.master.after(0, lambda: self._update_geo_display(geo_data))
    
    def _update_geo_display(self, geo_data):
        """Update the geo display with the processed data (called in main thread)"""
        # Populate tree using TreeViewManager
        gui.tree_manager.populate_tree(self.geo_tree, geo_data)
        self.update_output(f"Showing geolocation data for {len(geo_data)} IP addresses")
    
    def show_location_details(self, event):
        """Show details for selected IP location"""
        # Clear existing items
        gui.tree_manager.clear_tree(self.geo_details_tree)
        
        # Get selected IP
        selected = self.geo_tree.selection()
        if not selected:
            return
            
        values = self.geo_tree.item(selected[0], "values")
        ip = values[0]
        
        self.selected_ip_var.set(ip)
        
        # Get detailed location information
        location = self.get_ip_location(ip)
        
        # Add IP alerts count information
        def get_ip_alert_count():
            cursor = gui.db_manager.analysis_conn.cursor()
            count = cursor.execute(
                "SELECT COUNT(*) FROM alerts WHERE ip_address = ?", 
                (ip,)
            ).fetchone()[0]
            
            # Get most recent alert timestamp
            cursor.execute(
                "SELECT timestamp FROM alerts WHERE ip_address = ? ORDER BY timestamp DESC LIMIT 1",
                (ip,)
            )
            result = cursor.fetchone()
            last_seen = result[0] if result else "Never"
            
            # Format timestamp if it's a number
            if isinstance(last_seen, (int, float)):
                last_seen = datetime.fromtimestamp(last_seen).strftime('%Y-%m-%d %H:%M:%S')
                
            return count, last_seen
        
        # Queue the database query
        gui.db_manager.queue_query(
            get_ip_alert_count,
            callback=lambda result: self._update_location_details(ip, location, result)
        )
    
    def _update_location_details(self, ip, location, alert_info):
        """Update the location details with alert information"""
        alert_count, last_seen = alert_info
        
        # Database source info
        database_source = location.get("database", "Unknown")
        
        # Prepare details data
        details = [
            ("IP", ip),
            ("Database", database_source),
            ("Country", location.get("country", "Unknown")),
            ("City", location.get("city", "Unknown")),
            ("Region", location.get("region", "Unknown")),
            ("Postal Code", location.get("postal", "Unknown")),
            ("Latitude", f"{location.get('latitude', 0):.4f}"),
            ("Longitude", f"{location.get('longitude', 0):.4f}"),
            ("Timezone", location.get("timezone", "Unknown")),
            ("Alert Count", str(alert_count)),
            ("Last Seen", last_seen),
            ("Network Type", "Local Network" if location.get("is_local", False) else "External Network")
        ]
        
        # Populate details tree
        gui.tree_manager.populate_tree(self.geo_details_tree, details)
        
        # Queue query to get alert types for this IP
        def get_ip_alert_types():
            cursor = gui.db_manager.analysis_conn.cursor()
            return cursor.execute("""
                SELECT rule_name, COUNT(*) as count
                FROM alerts
                WHERE ip_address = ?
                GROUP BY rule_name
                ORDER BY count DESC
            """, (ip,)).fetchall()
        
        # Queue the query
        gui.db_manager.queue_query(
            get_ip_alert_types,
            callback=lambda rows: self._add_alert_type_details(rows)
        )
        
        self.update_output(f"Showing location details for IP: {ip}")
    
    def _add_alert_type_details(self, rows):
        """Add alert type information to the details display"""
        if not rows:
            return
            
        # Add a separator
        gui.tree_manager.populate_tree(self.geo_details_tree, [("", "")])
        gui.tree_manager.populate_tree(self.geo_details_tree, [("Alert Types", "")])
        
        # Add each alert type
        for rule_name, count in rows:
            gui.tree_manager.populate_tree(self.geo_details_tree, [(f"  {rule_name}", str(count))])
    
    def show_map(self):
        """Show world map with alert locations using downloaded or local map image"""
        if not self.city_reader and not self.country_reader:
            messagebox.showwarning("No GeoIP Database", 
                                 "Please load GeoIP databases first")
            return
        
        # Check if we have a map image
        if not self.map_image:
            # If not, show a message that we're using a simple map
            messagebox.showinfo("Map Image", 
                              "No map image available. Using a simplified map view.")
            
            # Use the original map drawing code as fallback
            self._show_simple_map()
            return
            
        # Get data for the map
        selected_ips = []
        selected = self.geo_tree.selection()
        
        if selected:
            # If an IP is selected, show just that one
            ip = self.geo_tree.item(selected[0], "values")[0]
            selected_ips.append(ip)
        else:
            # Otherwise show all visible IPs (up to 30)
            for item_id in self.geo_tree.get_children()[:30]:
                ip = self.geo_tree.item(item_id, "values")[0]
                selected_ips.append(ip)
        
        if not selected_ips:
            messagebox.showinfo("No Data", "No IP addresses to display on map")
            return
        
        # Create a map window
        map_window = tk.Toplevel(gui.master)
        map_window.title("IP Location Map")
        map_window.geometry("1000x700")
        
        # Create a frame for the map with a canvas
        map_frame = ttk.Frame(map_window)
        map_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Create canvas for the map
        canvas = tk.Canvas(map_frame, bg="white")
        canvas.pack(fill="both", expand=True)
        
        # Create a copy of the map image to draw on
        map_copy = self.map_image.copy()
        
        # Get the aspect ratio of the image
        img_width, img_height = map_copy.size
        
        # Resize the image to fit the window while maintaining aspect ratio
        canvas_width = 950
        canvas_height = 550
        
        # Calculate the image scaling factor
        scale_factor = min(canvas_width / img_width, canvas_height / img_height)
        new_width = int(img_width * scale_factor)
        new_height = int(img_height * scale_factor)
        
        # Resize the image
        resized_map = map_copy.resize((new_width, new_height), Image.LANCZOS)
        
        # Convert the image to PhotoImage for tkinter
        self.map_photo = ImageTk.PhotoImage(resized_map)
        
        # Draw the map on the canvas
        canvas.create_image(canvas_width/2, canvas_height/2, image=self.map_photo)
        
        # Draw IP locations on the map
        for ip in selected_ips:
            location = self.get_ip_location(ip)
            
            # Skip IPs with no location data
            if location.get("is_local", False):
                continue
                
            lat = location.get("latitude", 0)
            lon = location.get("longitude", 0)
            
            # Skip if no valid coordinates
            if lat == 0 and lon == 0:
                continue
                
            # Convert lat/lon to x,y coordinates
            # Map coordinates: lat [-90,90] -> y [bottom, top], lon [-180,180] -> x [left, right]
            x = canvas_width / 2 + (lon / 180.0 * canvas_width / 2)
            y = canvas_height / 2 - (lat / 90.0 * canvas_height / 2)
            
            # Determine dot color based on database source
            dot_color = "#ff6666"  # Red for city database
            if location.get("database") == "Country":
                dot_color = "#ff9933"  # Orange for country-only lookups
            
            # Draw dot for IP
            dot_id = canvas.create_oval(x-5, y-5, x+5, y+5, fill=dot_color, outline="black")
            
            # Create tooltip with IP information
            tooltip_text = f"{ip}: {location.get('country', '')}/{location.get('city', '')}"
            
            # Bind mouse events for tooltip
            def show_tooltip(event, text=tooltip_text, dot=dot_id):
                # Create tooltip near the dot
                x, y = canvas.coords(dot)[:2]
                tip = canvas.create_text(x+10, y-15, text=text, fill="black", 
                                         font=("Arial", 8), anchor="w",
                                         tags=f"tooltip_{dot}")
                # Create background for better readability
                bbox = canvas.bbox(tip)
                bg = canvas.create_rectangle(bbox, fill="white", outline="black", 
                                           tags=f"tooltip_bg_{dot}")
                canvas.tag_lower(bg, tip)
            
            def hide_tooltip(event, dot=dot_id):
                # Remove the tooltip
                canvas.delete(f"tooltip_{dot}")
                canvas.delete(f"tooltip_bg_{dot}")
            
            canvas.tag_bind(dot_id, "<Enter>", show_tooltip)
            canvas.tag_bind(dot_id, "<Leave>", hide_tooltip)
        
        # Add a legend
        legend_frame = ttk.LabelFrame(map_window, text="Legend")
        legend_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(legend_frame, text="Red: City Database").pack(side="left", padx=20)
        ttk.Label(legend_frame, text="Orange: Country Database").pack(side="left", padx=20)
        ttk.Label(legend_frame, text="Local/Private IPs not shown").pack(side="left", padx=20)
        
        # Add stats about shown IPs
        shown_ips = sum(1 for ip in selected_ips 
                       if not self.get_ip_location(ip).get("is_local", False) 
                       and (self.get_ip_location(ip).get("latitude", 0) != 0 
                            or self.get_ip_location(ip).get("longitude", 0) != 0))
                       
        ttk.Label(legend_frame, 
                 text=f"Showing {shown_ips} of {len(selected_ips)} IP addresses").pack(side="right", padx=20)
        
        # Add close button
        ttk.Button(map_window, text="Close", command=map_window.destroy).pack(pady=10)
    
    def _show_simple_map(self):
        """Fallback method that shows a simple drawn map if no image is available"""
        # Get data for the map
        selected_ips = []
        selected = self.geo_tree.selection()
        
        if selected:
            # If an IP is selected, show just that one
            ip = self.geo_tree.item(selected[0], "values")[0]
            selected_ips.append(ip)
        else:
            # Otherwise show all visible IPs (up to 20)
            for item_id in self.geo_tree.get_children()[:20]:
                ip = self.geo_tree.item(item_id, "values")[0]
                selected_ips.append(ip)
        
        if not selected_ips:
            messagebox.showinfo("No Data", "No IP addresses to display on map")
            return
            
        # Create a map window
        map_window = tk.Toplevel(gui.master)
        map_window.title("IP Location Map")
        map_window.geometry("800x600")
        
        # Simple SVG-style world map visualization
        canvas = tk.Canvas(map_window, bg="white")
        canvas.pack(fill="both", expand=True)
        
        # Draw simplified world map background (rectangle with blue color)
        canvas.create_rectangle(50, 50, 750, 550, fill="#e6f7ff", outline="#cccccc")
        
        # Draw simple continent outlines
        # These are very simplified representations
        # North America
        canvas.create_polygon(100, 150, 250, 150, 300, 300, 150, 400, 100, 300, fill="#d9d9d9", outline="#999999")
        # South America
        canvas.create_polygon(250, 350, 300, 350, 350, 500, 250, 500, fill="#d9d9d9", outline="#999999")
        # Europe
        canvas.create_polygon(400, 150, 450, 150, 450, 250, 400, 250, fill="#d9d9d9", outline="#999999")
        # Africa
        canvas.create_polygon(400, 250, 450, 250, 450, 400, 400, 450, 350, 400, 350, 300, fill="#d9d9d9", outline="#999999")
        # Asia
        canvas.create_polygon(450, 150, 650, 150, 650, 350, 450, 350, fill="#d9d9d9", outline="#999999")
        # Australia
        canvas.create_polygon(650, 350, 700, 350, 700, 400, 650, 400, fill="#d9d9d9", outline="#999999")
        
        # Draw coordinates grid
        for i in range(100, 700, 100):
            # Vertical lines
            canvas.create_line(i, 50, i, 550, fill="#cccccc", dash=(2, 4))
            # Horizontal lines
            canvas.create_line(50, i, 750, i, fill="#cccccc", dash=(2, 4))
        
        # Draw IP locations on the map
        ip_count = 0
        for ip in selected_ips:
            location = self.get_ip_location(ip)
            
            # Skip local IPs
            if location.get("is_local", False):
                continue
                
            lat = location.get("latitude", 0)
            lon = location.get("longitude", 0)
            
            # Skip if no valid coordinates
            if lat == 0 and lon == 0:
                continue
                
            # Convert lat/lon to x,y coordinates
            # Map coordinates: lat [-90,90] -> y [550,50], lon [-180,180] -> x [50,750]
            x = 400 + (lon / 180.0 * 350)
            y = 300 - (lat / 90.0 * 250)
            
            # Determine dot color based on database source
            dot_color = "red"
            if location.get("database") == "Country":
                dot_color = "orange"  # Use orange for country-only lookups
            
            # Draw dot for IP
            canvas.create_oval(x-5, y-5, x+5, y+5, fill=dot_color, outline="black")
            
            # Draw IP label
            canvas.create_text(x, y-15, text=ip, font=("Arial", 8))
            
            # Draw country/city label if available
            location_text = f"{location.get('country', '')}/{location.get('city', '')}"
            if location_text != "/":
                canvas.create_text(x, y+15, text=location_text, font=("Arial", 8))
                
            ip_count += 1
        
        # Add a legend
        canvas.create_text(100, 50, text="IP Location Map", font=("Arial", 12, "bold"), anchor="w")
        canvas.create_oval(100, 65, 110, 75, fill="red", outline="black")
        canvas.create_text(120, 70, text="IP Location (City DB)", font=("Arial", 10), anchor="w")
        
        canvas.create_oval(100, 85, 110, 95, fill="orange", outline="black")
        canvas.create_text(120, 90, text="IP Location (Country DB only)", font=("Arial", 10), anchor="w")
        
        canvas.create_text(400, 30, text="Simplified Map (Local IPs not shown)", 
                          font=("Arial", 10, "italic"))
        
        # Add close button
        ttk.Button(map_window, text="Close", command=map_window.destroy).pack(pady=10)
        
        self.update_output(f"Showing simplified map with {ip_count} located IP addresses")