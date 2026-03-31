FROM jupyter/scipy-notebook:latest

LABEL maintainer="RENCI MISM Team"
LABEL description="Jupyter Lab with vivarium-core for MISM PoC"

# Install vivarium-core and visualization deps
RUN pip install --no-cache-dir \
    vivarium-core \
    matplotlib \
    numpy \
    && pip cache purge

# Copy sample notebooks into the default work directory
COPY notebooks/ /home/jovyan/notebooks/

# Set working directory to notebooks
WORKDIR /home/jovyan
