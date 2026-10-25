# Cinematic Break Pilot System

Cinematic Break is a secure, low-latency media streaming service designed for movie theaters. It runs as a Dockerized server on a mini-PC connected to the theater's local network, allowing patrons to seamlessly sync the ongoing movie to their personal devices.

---

## What is Cinematic Break?

Leaving a movie to use the restroom, grab concessions, or take an urgent call usually means missing important parts of the film. Cinematic Break solves this by letting patrons stream the ongoing movie on their own phone or tablet while away from their seats.

The system runs entirely on the theater's local area network (LAN) rather than over the internet, keeping network latency under 10ms with zero cloud streaming costs. Playback is continuously synced to the auditorium's timeline, and access is protected by built-in ticket validation, on-the-fly stream encryption, and tamper-resistant watermarks.

### Key Security & Sync Features
- **Ultra Low-Latency Sync:** Sub-10ms delay using client-side Network Round-Trip Time (RTT) latency estimation to perfectly align device clocks.
- **Ticket Validation System:** Built-in 6-digit ticket generator and validator to prevent unauthorized access (designed to be easily swapped out for a 3rd-party POS database later).
- **Stream Security (DRM):** AES-128 on-the-fly HLS stream encryption preventing raw video scraping.
- **Client-Side Watermarking:** Dynamic floating watermark overlays identifying the specific viewer's ticket number to deter screen recording.
- **Tamper Detection & Auto-Ban:** A MutationObserver script watches the DOM. If a user deletes the watermark in dev tools, the backend instantly revokes the ticket, destroys their stream, and blacklists them.

---

## How to Use It

The system has two main user flows: one for theater staff managing sessions, and one for patrons watching on their phones.

### For Theater Staff (Admin)

1. **Initial Admin Setup:** When you run the server for the first time, open `http://localhost:8000` (or your host machine's IP). You'll be prompted at `/setup` to create the initial admin account. After that, log in through `/login`.
2. **Adding Movies:** Put your video files (`.mp4`, `.mkv`, etc.) in the `media/movies/` folder. The server scans this directory automatically and reads the video duration and codec using `ffprobe`.
3. **Starting a Session:** On the dashboard, find your movie and click the play button on its poster. Enter a theater/auditorium number (e.g., "Auditorium 3") and start the session. The server creates an 8-digit join code, generates a pool of 6-digit ticket codes, and prepares the stream.
4. **Showing the QR Code:** Click the QR code icon on any active session card to view its QR code. You can also open `http://localhost:8000/theater/<number>` on a lobby display so patrons can scan it as they exit.
5. **Session Controls:** You can pause, resume, or stop any session directly from its card. Use the **Pause All** and **Stop All** buttons in the header to control all active screens at once.
6. **Ticket Moderation:** Click **Tickets** on an active session to see all generated ticket numbers, check which ones are currently streaming, or manually end or ban a ticket.
7. **Settings:** Under `/settings`, you can set the External DNS / Local IP (so generated QR codes point to your LAN IP instead of localhost), change the break timeout limit (default is 10 minutes), or update your admin password.

### For Theater Patrons

1. **Connect to Wi-Fi:** Join the theater's guest Wi-Fi on your phone or tablet.
2. **Join the Movie:** Scan the QR code posted near the exit, or navigate to `/join` in your browser and enter the 8-digit session code.
3. **Enter Ticket Number:** Enter the 6-digit ticket code from your movie ticket stub.
4. **Watch in Sync:** The player automatically fast-forwards to the exact second the movie is playing on the theater screen and uses network latency compensation to stay in sync. Controls include volume, mute, fullscreen, and aspect ratio (Fit, Crop, Stretch).
5. **Pauses:** If theater staff pauses the movie in the auditorium, your player pauses automatically with an on-screen notice until playback resumes.
6. **Break Limit:** When your break time reaches the theater's timeout limit (default 10 minutes), the stream automatically stops and reminds you to head back to your seat.
7. **Watermark Notice:** A translucent watermark with your ticket number continuously bounces across the screen to deter recording. If someone tries to remove or hide the watermark using developer tools, the player detects it, bans the ticket, and cuts off the stream.

---

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

---

## Release Notes

### What's Working in This Submission

Based on the Git commit history, here is what is currently implemented and running:

- **Edge Server & Docker Environment:** The application runs in a Docker container via Docker Compose with an optimized `.dockerignore`. It automatically scans `media/movies/` for video files and manages state using SQLite (`sessions`, `settings`, `tickets`, and `admin_users` tables).
- **Streaming & Codec Handling:** Supports native MP4 streaming using HTTP 206 byte-range requests for instant seeking, as well as on-the-fly HLS transcoding via FFmpeg for `.mkv` files. Non-H.264 formats (such as HEVC/H.265) are automatically transcoded to standard H.264 so they play without issues in mobile browsers.
- **Low-Latency Timeline Synchronization:** The server acts as the master clock for movie progress. The client player polls `/sessions/<id>/state` and calculates round-trip time (RTT) to compensate for one-way network latency, keeping playback synced to the theater screen within 10ms. Pausing and resuming from the admin dashboard syncs across all connected devices immediately.
- **Security & DRM Suite:**
  - HLS video chunks are encrypted on-the-fly with AES-128. Decryption keys are served from `/sessions/<id>/key.bin` only to devices with a valid ticket cookie.
  - Built-in 6-digit ticket system ties each stream to an authorized ticket code.
  - A bouncing frosted-glass watermark displaying the patron's ticket number floats over the video.
  - A client-side watchdog (MutationObserver and computed-style checker) detects if the watermark is deleted, hidden, or modified via dev tools, immediately revokes the ticket, and terminates the stream.
- **Admin Dashboard & Authentication:** Includes a first-time setup page (`/setup`) with bcrypt password hashing and a password strength meter, secure login/logout, brute-force IP rate limiting, master pause/stop controls, a ticket management modal to view or ban connected devices, and a settings page to configure local IP and break timeout duration.

### Milestone Status & Project Plan Comparison

In our Capstone Project Plan (`Project Plan - Computah - CINEMATIC.docx`), project work was scheduled across three milestones:
- **Milestone 1:** WBS 1.0 (Environment & Infrastructure) & WBS 2.0 (Authentication & Session Management)
- **Milestone 2:** WBS 3.0 (Edge Platform & Synchronization) & WBS 5.0 (Administrator Portal)
- **Milestone 3:** WBS 4.0 (Media Playback & Security) & WBS 6.0 (Integration & Validation)

#### Notes & Explanations:

- **Milestones 1 & 2 are complete:** The Docker container, SQLite database, QR code access, session management, timeline synchronization, and admin dashboard are all built and functional.
- **Milestone 3 security features delivered early:** In the original schedule, DRM encryption, dynamic watermarking, and anti-tamper mechanisms were slated for Milestone 3. However, during our initial meetings with project sponsor Michael Urban, piracy and media protection were identified as the single biggest concern for theater operators and film studios. Because of this, our team prioritized building the AES-128 encryption, dynamic watermarking, and auto-ban systems ahead of schedule.
- **Geo-fencing approach:** The project plan initially mentioned device-level GPS geo-fencing (WBS 2.0). In practice, GPS signals are weak or non-existent inside concrete movie theaters. To avoid false lockouts, we enforce physical presence by running the server strictly on the theater's local Wi-Fi network, pairing it with auditorium-specific join codes and 6-digit ticket validation.
- **HEVC / Video Compatibility:** We expanded video handling beyond basic MP4 files to detect HEVC/H.265 content and transcode it on the fly, avoiding compatibility problems on mobile devices.
