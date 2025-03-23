from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel, Field
from typing import List, Tuple, Dict, Any, Optional
import googlemaps
from haversine import haversine
import folium
import polyline
import uvicorn
import os
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Carpooling API", description="API for matching passengers with drivers for carpooling")

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get API key from environment variable for security
# You'll need to set this in your Render environment variables
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Create a static folder to store generated maps
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize Google Maps client
gmaps = None

@app.on_event("startup")
async def startup_event():
    global gmaps
    if not API_KEY:
        print("WARNING: No Google Maps API key found. Set GOOGLE_MAPS_API_KEY environment variable.")
    else:
        gmaps = googlemaps.Client(key=API_KEY)

# Define data models
class Location(BaseModel):
    address: str
    coordinates: Optional[Tuple[float, float]] = None

class Passenger(BaseModel):
    name: str
    origin: Location
    destination: Location

class Driver(BaseModel):
    origin: str
    destination: str
    seats_available: int = Field(gt=0, description="Number of available seats")

class MatchRequest(BaseModel):
    driver: Driver
    passengers: List[Passenger]
    tolerance_km: float = 2.0

class MatchResponse(BaseModel):
    matched_passengers: List[Dict[str, Any]]
    unmatched_passengers: List[Dict[str, Any]]
    seats_remaining: int
    map_url: Optional[str] = None

# Helper functions from your original code
def offset_coords(coord, index, offset=0.0003):
    return (coord[0] + (index * offset), coord[1] + (index * offset))

def get_route_coords(origin, destination):
    if not gmaps:
        raise HTTPException(status_code=500, detail="Google Maps API not initialized")
    
    directions = gmaps.directions(origin, destination, mode="driving")
    if not directions:
        return []

    overview_polyline = directions[0]['overview_polyline']['points']
    coords = polyline.decode(overview_polyline)
    return coords

def is_passenger_match(driver_route, passenger_origin, passenger_destination, tolerance_km=2.0):
    origin_near = any(haversine(passenger_origin, point) <= tolerance_km for point in driver_route)
    dest_near = any(haversine(passenger_destination, point) <= tolerance_km for point in driver_route)
    return origin_near and dest_near

def geocode_address(address):
    if not gmaps:
        raise HTTPException(status_code=500, detail="Google Maps API not initialized")
    
    result = gmaps.geocode(address)
    if result:
        location = result[0]['geometry']['location']
        return (location['lat'], location['lng'])
    return None

def generate_map(map_id, driver_route, matches, unmatched):
    if not driver_route:
        return None

    midpoint = driver_route[len(driver_route)//2]
    m = folium.Map(location=midpoint, zoom_start=7)

    folium.PolyLine(driver_route, color="blue", weight=5, opacity=0.7, tooltip="Driver Route").add_to(m)
    folium.Marker(driver_route[0], popup="Driver Start", icon=folium.Icon(color="blue")).add_to(m)

    for i, p in enumerate(matches):
        pickup_offset = offset_coords(p['origin_coords'], i)
        dropoff_offset = offset_coords(p['destination_coords'], i)
        folium.Marker(pickup_offset, popup=f"{p['name']} Pickup (Matched)", icon=folium.Icon(color="green")).add_to(m)
        folium.Marker(dropoff_offset, popup=f"{p['name']} Dropoff (Matched)", icon=folium.Icon(color="green")).add_to(m)

    for i, p in enumerate(unmatched):
        if 'origin_coords' in p and 'destination_coords' in p:
            pickup_offset = offset_coords(p['origin_coords'], i)
            dropoff_offset = offset_coords(p['destination_coords'], i)
            folium.Marker(pickup_offset, popup=f"{p['name']} Pickup (Unmatched)", icon=folium.Icon(color="red")).add_to(m)
            folium.Marker(dropoff_offset, popup=f"{p['name']} Dropoff (Unmatched)", icon=folium.Icon(color="red")).add_to(m)

    # Save map to static folder
    map_path = f"static/carpool_map_{map_id}.html"
    m.save(map_path)
    return map_path.replace("static/", "")

# API endpoints
@app.get("/")
async def root():
    return {"message": "Carpooling API is running. Use /docs for API documentation."}

@app.post("/match/", response_model=MatchResponse)
async def match_passengers(request: MatchRequest = Body(...)):
    if not gmaps:
        raise HTTPException(status_code=500, detail="Google Maps API not initialized")
    
    # Get driver route
    driver_route = get_route_coords(request.driver.origin, request.driver.destination)
    if not driver_route:
        raise HTTPException(status_code=404, detail="Could not find a route for the driver")
    
    seats_available = request.driver.seats_available
    matched = []
    unmatched = []
    
    # Process each passenger
    for p in request.passengers:
        if seats_available == 0:
            # Add to unmatched if no seats left
            unmatched.append({
                "name": p.name,
                "origin_address": p.origin.address,
                "destination_address": p.destination.address,
                "reason": "No seats available"
            })
            continue
            
        # Geocode addresses if coordinates not provided
        origin_coords = p.origin.coordinates if p.origin.coordinates else geocode_address(p.origin.address)
        dest_coords = p.destination.coordinates if p.destination.coordinates else geocode_address(p.destination.address)
        
        if not origin_coords or not dest_coords:
            unmatched.append({
                "name": p.name,
                "origin_address": p.origin.address,
                "destination_address": p.destination.address,
                "reason": "Could not geocode addresses"
            })
            continue
            
        # Check if passenger is a match
        if is_passenger_match(driver_route, origin_coords, dest_coords, request.tolerance_km):
            matched.append({
                "name": p.name,
                "origin_address": p.origin.address,
                "destination_address": p.destination.address,
                "origin_coords": origin_coords,
                "destination_coords": dest_coords
            })
            seats_available -= 1
        else:
            unmatched.append({
                "name": p.name,
                "origin_address": p.origin.address,
                "destination_address": p.destination.address,
                "origin_coords": origin_coords,
                "destination_coords": dest_coords,
                "reason": "Not on driver's route"
            })
    
    # Generate map with a unique ID
    import uuid
    map_id = str(uuid.uuid4())
    map_file = generate_map(map_id, driver_route, matched, unmatched)
    
    map_url = f"/static/{map_file}" if map_file else None
    
    return {
        "matched_passengers": matched,
        "unmatched_passengers": unmatched,
        "seats_remaining": seats_available,
        "map_url": map_url
    }

@app.get("/map/{map_file}", response_class=HTMLResponse)
async def get_map(map_file: str):
    try:
        with open(f"static/{map_file}", "r") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Map not found")

# Run the app locally (not needed for Render deployment)
if __name__ == "__main__":
    # Use port from environment variable for services like Render
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)   
