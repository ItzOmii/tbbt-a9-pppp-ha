FROM homeassistant/home-assistant:stable

# TBBT PPPP battery cams (TBBT-665567-AMFSR / TBBT-681302-FNECR) need aiopppp patched:
# 010A start bytes, plain-text login, unencoded 'ff 00 00 00 <ticket>' login ack.
# Without it the camera acks VideoParamSet but never starts video (no MJPEG stream).
# aiopppp is normally installed at first start by the pppp_camera custom component,
# so install it here and patch it in the image instead.
RUN pip3 install --no-cache-dir aiopppp==0.2.2

COPY patch_aiopppp_tbbt.py /tmp/patch_aiopppp_tbbt.py
RUN python3 /tmp/patch_aiopppp_tbbt.py && rm /tmp/patch_aiopppp_tbbt.py
