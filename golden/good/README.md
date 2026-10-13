# Task Tracker API

A small REST API for managing tasks, built with Flask.

## Overview
Users can create, list, complete and delete tasks. Data is stored in SQLite so there is nothing to configure.

## Installation
```
pip install -r requirements.txt
```

## Usage
Run the server with `python -m app.main` and open http://localhost:5000/tasks.

## Testing
Run `pytest` from the project root. Tests cover creation, completion and validation errors.

## Architecture
`app/models.py` holds the Task model and persistence, `app/main.py` holds the HTTP routes.
