# Cinematic Break Pilot System

## Overview
The Cinematic Break Pilot System is a secure, low-latency media streaming service designed for movie theaters. It runs as a Dockerized server on a mini-PC within the theater network and allows patrons to seamlessly sync the ongoing movie to their devices (via a web browser) when they need to step out (e.g., for popcorn or the restroom), ensuring they don't miss any of the film.

Each theater hosts a session dedicated strictly to the duration of the movie playing. This server extends the movie playing to devices for people temporarily outside the theater. When the movie ends, the session ends immediately, providing a secure and ephemeral viewing experience.

## Core Features
- **No-Latency Syncing:** The stream must be perfectly synchronized with the theater's main screen timeline. The system uses WebSocket-based time synchronization and adaptive bitrate streaming (like HLS or WebRTC) to minimize delay.
- **Easy Web Access:** Patrons access the stream via a mobile-friendly website—no app download required. Users join the session by either scanning a QR code or entering a temporary 8-digit alphanumeric code.
- **End-to-End Encryption:** To prevent unauthorized interception on the local network, the stream data is encrypted in transit using TLS, and the media segments themselves can be encrypted (e.g., HLS with AES-128 encryption).
- **Anti-Piracy DRM:** Integration with web-based DRM frameworks (like Widevine for Chrome/Android or FairPlay for Safari/iOS) to restrict screen recording, mirroring, and external display output from the device.
- **Dynamic Watermarking:** The video stream features a robust, invisible (or subtly visible) watermark unique to the user's ephemeral session. In the event of screen capture via external cameras, the leak can be traced back to the specific patron's session.

## System Architecture

### 1. Server (Mini-PC)
- **Environment:** Docker container for easy installation and isolation.
- **Tech Stack:** Python (FastAPI/Flask or similar) for the control plane and API.
- **Media Engine:** A streaming engine like FFmpeg or GStreamer to transcode and segment the theater's source feed into an encrypted streaming format.
- **Session Management:** Ephemeral sessions that begin at the start of the film and are forcefully terminated the moment the film ends. 

### 2. Client (Web Application)
- **Environment:** Modern web browsers (Safari, Chrome, Firefox).
- **Tech Stack:** HTML/JS framework (e.g., React, Vue, or vanilla JS).
- **Functionality:** 
  - Connects to the local theater Wi-Fi (if required) or public endpoint.
  - Authenticates via an 8-digit alphanumeric code or a scanned QR code displayed at the theater.
  - Plays the encrypted stream directly in the browser using HTML5 Encrypted Media Extensions (EME).
  - Reports telemetry for sync adjustment to stay perfectly aligned with the theater projection.

## Folder Structure
- `/app/`: The Python-based backend server handling API requests, ephemeral session logic, and stream generation.
- `/web/`: The web application source code containing the QR/Code entry portal and the video player.
- `/config/`: Configuration files for the server and streaming engine.
- `Dockerfile` & `docker-compose.yml`: For deploying the server environment.

## Next Steps
1. Define the API schema for the 8-digit code/QR authentication and time-syncing.
2. Set up the Docker environment and a basic streaming server.
3. Implement the web-based media player with EME (DRM) support and watermarking overlays.
