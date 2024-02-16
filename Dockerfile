# Use Python 3.8-slim as the base image
FROM python:3.8-slim

# Install bgpdump
RUN apt-get update && apt-get install -y --no-install-recommends \
    bgpdump \
    && rm -rf /var/lib/apt/lists/*

# Install python modules
RUN pip3 install numpy networkx pandas gprof2dot psutil requests-cache

# Set the project
WORKDIR /app
COPY main.py rib_table.py url_generation.py .
COPY observers ./observers
COPY configs ./configs

ENTRYPOINT ["python3", "main.py"]


# Build with
# docker build -t quickrib .

# Development container
# docker run --name quickrib_dev -it -v $(pwd):/app --entrypoint bash quickrib

# Production container
# docker run --rm --name quickrib_prod -v $(pwd)/config.py:/app/configs/config.py -v $(pwd):/app/data quickrib
