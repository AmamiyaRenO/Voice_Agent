cmake_minimum_required(VERSION 3.20)
project(webrtc_apm_unity LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(webrtc_apm_unity SHARED
  src/webrtc_apm_unity.cpp
)

target_include_directories(webrtc_apm_unity PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(WEBRTC_APM QUIET webrtc-audio-processing)
endif()

set(WEBRTC_APM_INCLUDE_DIR "" CACHE PATH "Include directory containing modules/audio_processing/include/audio_processing.h")
set(WEBRTC_APM_LIBRARY "" CACHE FILEPATH "Library file for WebRTC APM (e.g., webrtc_audio_processing)")

if(WEBRTC_APM_FOUND)
  message(STATUS "Found webrtc-audio-processing via pkg-config")
  target_include_directories(webrtc_apm_unity PRIVATE ${WEBRTC_APM_INCLUDE_DIRS})
  target_link_libraries(webrtc_apm_unity PRIVATE ${WEBRTC_APM_LIBRARIES})
  if(WEBRTC_APM_CFLAGS_OTHER)
    target_compile_options(webrtc_apm_unity PRIVATE ${WEBRTC_APM_CFLAGS_OTHER})
  endif()
else()
  if(WEBRTC_APM_INCLUDE_DIR STREQUAL "" OR WEBRTC_APM_LIBRARY STREQUAL "")
    message(FATAL_ERROR
      "Could not locate webrtc-audio-processing.\n"
      "Either install a package that provides pkg-config 'webrtc-audio-processing',\n"
      "or set -DWEBRTC_APM_INCLUDE_DIR and -DWEBRTC_APM_LIBRARY."
    )
  endif()
  target_include_directories(webrtc_apm_unity PRIVATE ${WEBRTC_APM_INCLUDE_DIR})
  target_link_libraries(webrtc_apm_unity PRIVATE ${WEBRTC_APM_LIBRARY})
endif()

if(MSVC)
  target_compile_definitions(webrtc_apm_unity PRIVATE _CRT_SECURE_NO_WARNINGS)
endif()

