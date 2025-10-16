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

# Expose the default port for Koyeb (8000)
EXPOSE 8000

# Command to run the application using gunicorn. 
# Added --timeout 60 to prevent premature worker shutdown/health check failures.
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "60", "--bind", "0.0.0.0:8000", "app:app"]

