# Use an official Python runtime as a parent image.
FROM python:3.10-slim

# Set environment variables to prevent .pyc files and enable unbuffered output.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies required for building psycopg2.
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container.
WORKDIR /app

# Copy only the requirements file first to leverage Docker cache.
COPY requirements.txt /app/

# Upgrade pip and install dependencies.
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the application code.
COPY . /app/

# Expose the port the app runs on.
EXPOSE 8000

# Specify the command to run your app using Gunicorn.
CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:8000", "--workers", "3"]
