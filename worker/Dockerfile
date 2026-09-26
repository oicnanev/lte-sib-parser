FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# UHD/lime drivers
RUN apt update && apt install -y --no-install-recommends \
    --no-install-suggests \
    soapysdr-tools libsoapysdr-dev \
    soapysdr-module-lms7 \
    soapysdr-module-hackrf hackrf \
    soapysdr-module-rtlsdr \
    uhd-soapysdr \
    uhd-host libuhd-dev \
    # build deps
    git cmake libfftw3-dev libmbedtls-dev libboost-program-options-dev libconfig++-dev libsctp-dev \
    libzmq3-dev libboost-system-dev libboost-test-dev libboost-thread-dev \
    g++ make pkg-config libpython2-dev swig libi2c-dev \
    libboost-program-options-dev libconfig++-dev libusb-1.0-0-dev swig \
    && rm -rf /var/lib/apt/lists/*


ENV UHD_IMAGES_DIR=/usr/share/uhd/images/

RUN uhd_images_downloader

# Get srsLTE, compile and install
RUN git clone https://github.com/srsran/srsRAN_4G

#COPY ./srsRAN_4G /srsRAN_4G

COPY sib_logger.patch rx_only.patch /srsRAN_4G/

WORKDIR /srsRAN_4G/

# origin/master at 2024.12.25
# rx_only.patch: srsue never transmits and never retunes the LO for TX
# (required for half-duplex SDRs like HackRF, which share one LO)
RUN git checkout ec29b0c1ff79cebcbe66caa6d6b90778261c42b8 && git apply sib_logger.patch rx_only.patch && \
    mkdir build && cd build && \
    cmake -DUSE_LTE_RATES=ON ../ && make -j`nproc` cell_search srsue && make install && \
    ldconfig

RUN cp /srsRAN_4G/build/lib/examples/cell_search /usr/bin/

RUN mkdir /vol/

WORKDIR /vol

ENTRYPOINT ["bash", "-c", "cp /vol/helpers/uhd_images/*.bin /usr/share/uhd/images/ 2>/dev/null || true && bash"]