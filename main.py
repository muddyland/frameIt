import os, sys
from flask import Flask, render_template, request, Response, jsonify, send_from_directory
from datetime import datetime
import requests 
import subprocess
import json
import random

# Radarr API endpoint URL (replace with yours)
RADARR_API_URL = os.environ.get("RADARR_URL", 'http://localhost:7878') + "/api/v3"
# Radarr API key (replace with yours)
RADARR_API_KEY = os.environ.get("RADARR_KEY", None)

# Overseerr API endpoint URL (replace with yours)
OVERSEERR_API_URL = os.environ.get("OVERSEERR_URL", 'http://localhost:8080') + "/api/v1"
# Get an API token from your Tautulli instance and store it in a secure way
OVERSEERR_API_TOKEN = os.environ.get("OVERSEERR_TOKEN", None)

STATIC_DIR = './static' 
DATA_DIR = os.environ.get("DATA_DIR", './config')
IMAGES_DIR = os.environ.get("IMAGES_DIR", './images')

JSON_file = DATA_DIR + '/data.json'

app = Flask(__name__, static_url_path='/static', static_folder=STATIC_DIR)

def load_images_json():
    try:
        with open(JSON_file, 'r') as f:
            data = json.load(f)
            return data
    except FileNotFoundError:
        print("Error: images.json was not found.")
        return {"photos" : []}
    except json.JSONDecodeError:
        print("Error: unable to parse images.json file.")
        return {"photos" : []}

def save_images_json(data):
    try:
        with open(JSON_file, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Error saving images.json: {e}")
        
# Function to store uploaded image
def upload_image(image_data, filename):
    # Create ./static/img/ folder if it doesn't exist
    img_folder = IMAGES_DIR
    if not os.path.exists(img_folder):
        os.makedirs(img_folder)
    
    # Store the uploaded image in ./static/img/
    img_path = f"{img_folder}/{filename}"
    with open(img_path, 'wb') as f:
        f.write(image_data)
    
    return img_path

def get_radarr_media():
    print(f"Getting media from {RADARR_API_URL}")
    endpoint = "/history"
    params = {
        "page1" : 0,
        "pageSize" : 25,
        "includeMovie" : "true"
    }
    url = f"{RADARR_API_URL}{endpoint}"
    
    # Get the list of movies from Radarr
    try:
        response = requests.get(url, headers={"X-Api-Key": RADARR_API_KEY}, params=params)
        data  = json.loads(response.text)
        if data.get('records'):
            # get a random index from the list of movies
            index = random.randint(0, len(data["records"]) -1)
            
            # Get poster details for the movie at that index
            data   = data['records'][index]
            added_date = datetime.strptime(data['date'], '%Y-%m-%dT%H:%M:%SZ').strftime('%B %d, %Y')
            photos = data['movie']['images']
            for photo in photos:
                if photo["coverType"] == "poster":
                    image_url = photo['remoteUrl']
                    name = data['movie']['title']
                    break
                    
            
            if not name and image_url:
                print(f"{data['movie']['title']} has no poster")
                return None
            
            poster_details = {
                "name" : name,
                "path": image_url,
                "added_date" : added_date
            }
            return poster_details
        else:
            print("No media found in Radarr")
            return None
        
    except:
        e = sys.exc_info()
        print(f"Error getting Radarr media history: {e}")
        return []

def get_overseerr_media():
    print(f"Getting Overseerr media from {OVERSEERR_API_URL}")
    # Check if media is currently being played
    media_type = 'movie'
    endpoint = "/discover/movies/upcoming"
    url = f"{OVERSEERR_API_URL}{endpoint}"

    try:
        headers = {"X-Api-Key": OVERSEERR_API_TOKEN}
        response = requests.get(url, headers=headers)
        
        # Check if the response was successful
        if response.status_code != 200:
            return {"error": "Failed to fetch data. Status code: " + str(response.status_code) + " " + str(response.text)}, 500

        media_data = response.json()
        
        random_media_item = random.choice(media_data['results'])
        release_date = datetime.strptime(random_media_item["releaseDate"], "%Y-%m-%d")
        release_date = datetime.strftime(release_date, "%B %Y")
        poster_url = random_media_item['posterPath']
        
        imdb_url = f"https://image.tmdb.org/t/p/original{poster_url}"
        
        return {"media_type": media_type, "name": imdb_url, "path": imdb_url, "media_data": media_data, "release_date" : release_date}
    
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}, 500

# Function to update images.json with new image path
def update_images_json(new_img_path):
    data = load_images_json()
        
    if new_img_path in [i['name'] for i in data['photos']]:
        raise Exception(f"Image {new_img_path} already exists.")
    new_image = {
        "name": os.path.basename(new_img_path),
        "path": new_img_path.replace(IMAGES_DIR, '/images')
    }
    data['photos'].append(new_image)
    save_images_json(data)

def get_random():
    """
    Returns a random image from the 'images.json' file.
    """
    print("Getting random poster from local storage")
    images = load_images_json()
    
    # If 'images.json' exists and has photos, return one at random
    if len(images['photos']) > 0:
        index = random.randrange(len(images['photos']))        
        image = images['photos'][index]
        image['top_banner'] = "Now Playing"
        image['bottom_banner'] = "Tickets Available Now"
        return image
    
    # If 'images.json' exists but has no photos, return an empty list
    elif len(images['photos']) == 0:
        return {"path" : "/static/no-image.jpg", "name" : "No images are preset", "top_banner" : "No Images", "bottom_banner" : "Please add some images"}
    
    else:
        return {'error': 'Error returning an image...'}

@app.route('/' , methods=['GET'])
def frame():
    
    # Random choice for showing a picture, or getting one from overseerr
    sources = []
    
    if RADARR_API_KEY and RADARR_API_URL:
        sources.append('radarr')
    if OVERSEERR_API_TOKEN and OVERSEERR_API_URL:
        sources.append('overseerr')
    if load_images_json()['photos']:
        sources.append('db')
    
    print(f"Sources: {sources}")
    # Get a random choice of the above sources
    
    choice = random.choice(sources)
    
    # If choice is 'db' then get a picture from the database, else get one from overseerr
    if choice == 'overseerr':
        photo  = get_overseerr_media()
        if not photo:
            photo = get_random()
            top_banner = "Now Playing"
            bottom_banner = ""
        else:
            top_banner = "Coming Soon"
            bottom_banner = photo['release_date']
    elif choice == 'radarr':
        photo   = get_radarr_media()
        if not photo:
            photo = get_random()
            top_banner = "Now Playing"
            bottom_banner = ""
        else:
            top_banner  = "Recently Added"
            bottom_banner  = photo['added_date']
    else:
        photo = get_random()
        top_banner = photo['top_banner']
        bottom_banner = photo['bottom_banner']
    
    return render_template('frame.html', photo=photo, top_banner=top_banner, bottom_banner=bottom_banner)

@app.route('/admin')
def index():
    return render_template('index.html')

@app.route('/admin/upload', methods=['GET'])
def upload_html():
    return render_template('upload.html')

@app.route('/admin/list', methods=['GET'])
def list_html():
    return render_template('list.html')

#Needed for PWA
@app.route('/manifest.json')
def manifest():
    manifest_json = {
        "name": "FrameIT",
        "short_name": "FrameIT",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ff0000",
    }
    return jsonify(manifest_json)

@app.route('/api/images', methods=['GET'])
def get_images():
    """
    Returns a list of files stored in 'images.json'.
    
    The JSON file is expected to be in the following format:

    {"photos" : [{"name" : "Photo1", "path" : "filename123.jpg"}]}

    Returns:
        A Flask Response object with a JSON payload containing the list of files.
    """
    images = load_images_json()
    
    # If 'images.json' exists, return its contents as JSON
    if images['photos']:
        return jsonify(images['photos'])
    
    # If 'images.json' does not exist or has no photos, return an empty list
    else:
        return jsonify({'error': 'No images found'}), 404

@app.route('/api/images', methods=['POST'])
def add_image():
    """
    Adds a new image to the 'images.json' file.

    The request body is expected to be in JSON format with the following structure:

    {
      "name": "Photo1",
      "path": "filename123.jpg"
    }

    Returns:
        A Flask Response object with a JSON payload containing the updated list of files.
    """
    try:
        data = load_images_json()
        
        # Get the new image details from the request body
        new_image = {
            'name': request.json['name'],
            'path': request.json['path']
        }
        
        # Add the new image to the existing list
        data['photos'].append(new_image)
        
        # Save the updated JSON file
        save_images_json(data)
        
        return jsonify({'message': 'Image added successfully'}), 201
    
    except KeyError:
        return jsonify({'error': 'Missing required fields in request body'}), 400

@app.route('/api/images/upload', methods=['POST'])
def upload():
    """
    Uploads a new image and stores it in ./static/img/.

    Accepts multipart/form-data with a single file field.

    Returns:
        A Flask Response object indicating success.
    """
    file_name = request.files['file'].filename
    
    # Check to make sure file is an image
    file_extension = file_name.split('.')[-1].lower()
 
    # If the file extension isn't one of these, return a 400 error
    if not file_extension in ['jpg', 'jpeg', 'png']:
        return jsonify({'error': 'File must be .jpg, .png or .jpeg'}), 400
    # Store uploaded image
    img_path = upload_image(request.files['file'].read(), file_name)
    
    # Update images.json with new image path
    update_images_json(img_path)

    return jsonify({'message': 'Image uploaded successfully'}), 201



# Serve static files
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory(STATIC_DIR, path)

# Serve static files
@app.route('/images/<path:path>')
def send_images(path):
    return send_from_directory(IMAGES_DIR, path)

if __name__ == '__main__':
    
    # Example of how to use the function
    app.run(debug=True, host="0.0.0.0")