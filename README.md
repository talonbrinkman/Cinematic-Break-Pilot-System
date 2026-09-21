# Cinematic Break Pilot System

Cinematic Break Pilot System is a secure, low-latency media streaming service designed for movie theaters. It runs as a Dockerized server on a mini-PC within the theater network, allowing patrons to seamlessly sync the ongoing movie to their devices.

## Installation and Setup

The application is fully Dockerized for ease of deployment.

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) installed on your machine.
- [Docker Compose](https://docs.docker.com/compose/install/) (usually included with Docker Desktop).

### 1. Adding Test Media
Before starting the server, you need to add media to the `media/movies` folder. 
1. Open the repository root.
2. Ensure the directory `media/movies` exists. If not, create it.
3. Place your test media files (e.g. `.mp4`, `.mkv`) inside this directory. The server will recursively scan all subfolders inside the `media` directory.

### 2. Building and Running the Container
From the root of this repository, run the following command to build the image and start the container in the background:

```bash
docker-compose up -d --build
```

### 3. Accessing the Application
Once the container is running, the server will be available locally on port 8000.
Open your web browser and navigate to:
[http://localhost:8000](http://localhost:8000)

Here you will see the Admin dashboard displaying active sessions and your recently added media. Click the play button over a media poster to create a new active session!

#### Accessing from Other Devices (Phones, Tablets)
To access the app from another device on the same Wi-Fi network (like a patron's phone), you need to navigate to the host machine's Local IP Address instead of `localhost` (e.g., `http://192.168.1.15:8000`).

**Windows Networking Quirk:** If you are hosting on a Windows machine and other devices cannot connect (the page just spins), your Wi-Fi connection is likely set to **Public**. Windows Defender Firewall strictly blocks all incoming connections on "Public" networks.
To fix this:
1. Open Windows **Settings** -> **Network & internet** -> **Wi-Fi**.
2. Click your connected Wi-Fi network.
3. Change the "Network profile type" from **Public** to **Private**.

### Stopping the Server
To stop the server, run:
```bash
docker-compose down
```
