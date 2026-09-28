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
    # PSS/SSS search in vol/scripts/lte_pss.py
    python3-numpy \
    # build deps
    git cmake libfftw3-dev libmbedtls-dev libboost-program-options-dev libconfig++-dev libsctp-dev \
    libzmq3-dev libboost-system-dev libboost-test-dev libboost-thread-dev \
    g++ make pkg-config libpython2-dev swig libi2c-dev \
    libboost-program-options-dev libconfig++-dev libusb-1.0-0-dev swig \
    # libbladeRF build deps
    libcurl4-openssl-dev libedit-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/*

# libbladeRF from source: the Ubuntu 22.04 and Nuand PPA packages (2.4.1) predate
# bladeRF 2.0 firmware v2.6.0 / FPGA v0.16.0, whose RX metadata they mishandle.
# Release 2025.10 = libbladeRF 2.6.0, matching that firmware and FPGA. The FPGA
# image is loaded from the board's flash, so no bitstream is installed here.
RUN git clone --depth 1 --branch 2025.10 https://github.com/Nuand/bladeRF.git /tmp/bladeRF && \
    cd /tmp/bladeRF && git submodule update --init --depth 1 && \
    mkdir host/build && cd host/build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DINSTALL_UDEV_RULES=OFF -DBUILD_DOCUMENTATION=OFF .. && \
    make -j`nproc` && make install && ldconfig && rm -rf /tmp/bladeRF


ENV UHD_IMAGES_DIR=/usr/share/uhd/images/

RUN uhd_images_downloader

# Get srsLTE, compile and install
RUN git clone https://github.com/srsran/srsRAN_4G

#COPY ./srsRAN_4G /srsRAN_4G

COPY sib_logger.patch rx_only.patch cell_search_ppm.patch bladerf_rx.patch /srsRAN_4G/

WORKDIR /srsRAN_4G/

# origin/master at 2024.12.25
# rx_only.patch: srsue never transmits and never retunes the LO for TX
# (required for half-duplex SDRs like HackRF, which share one LO)
# cell_search_ppm.patch: adds -p <ppm> frequency correction to cell_search
# bladerf_rx.patch: bladeRF plugin reads RX continuously, without metadata
RUN git checkout ec29b0c1ff79cebcbe66caa6d6b90778261c42b8 && \
    git apply sib_logger.patch rx_only.patch cell_search_ppm.patch bladerf_rx.patch && \
    mkdir build && cd build && \
    cmake -DUSE_LTE_RATES=ON ../ && make -j`nproc` cell_search srsue srsran_rf_blade && make install && \
    ldconfig

RUN cp /srsRAN_4G/build/lib/examples/cell_search /usr/bin/

# lte_sib_decoder: MIB/SIB decoder on libsrsran's PHY, replaces srsue in the scans
COPY sib_decoder/lte_sib_decoder.cc /srsRAN_4G/sib_decoder/
RUN g++ -O2 -std=c++14 -I/usr/local/include -I/srsRAN_4G/build/lib/include \
      /srsRAN_4G/sib_decoder/lte_sib_decoder.cc -o /usr/bin/lte_sib_decoder -L/usr/local/lib \
      -Wl,--start-group -lrrc_asn1 -lasn1_utils -lsrsran_phy -lsrsran_common -lsrslog -Wl,--end-group \
      -lsrsran_rf -lfftw3f -lmbedcrypto -lpthread -lm

RUN mkdir /vol/

WORKDIR /vol

ENTRYPOINT ["bash", "-c", "cp /vol/helpers/uhd_images/*.bin /usr/share/uhd/images/ 2>/dev/null || true && bash"]