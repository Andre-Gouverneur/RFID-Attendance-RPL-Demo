# RFID Attendance System – RPL Demonstration Version

This repository contains selected source code and a sanitised sample database from an
RFID-based staff attendance system developed for IT Certification Academy (ITCA).

## Important privacy note

The original production database contained real staff information and attendance records.
That production database is **not included** in this public repository.

The file `attendance_sample.db` is a synthetic demonstration database that preserves the
project's database structure while using fictional names, RFID IDs, attendance records and
demo-only application accounts.

## Project technologies

- ESP32
- RC522 / MFRC522 RFID reader
- 16x2 LCD with I2C interface
- Wi-Fi
- Flask backend/API
- SQL / SQLite
- Docker
- HTML/CSS/JavaScript
- Visual Studio Code

## AI development assistance

Gemini AI was the primary AI development assistant, with additional assistance from
DeepSeek and ChatGPT. AI tools were used for wiring/code generation assistance,
debugging, explanation and development support. Hardware integration, testing,
troubleshooting, deployment decisions and evaluation of the system were performed by
the project developer.

## Database

`attendance_sample.db` contains synthetic example data only and must not be used for
production attendance records.

Demo accounts in the sample database:
- `demo_admin`
- `demo_user`

These are demonstration accounts only.
