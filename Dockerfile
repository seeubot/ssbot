# Use a Python base image suitable for production
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
# We install gunicorn here, which runs the application in production
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY app.py .

# Expose the default port for Koyeb (8000, though auto-detected)
EXPOSE 8000

# Command to run the application using gunicorn
# 'app:app' refers to: 'app.py' module : 'app' Flask instance name
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]

