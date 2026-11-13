cmake_minimum_required(VERSION 3.16)
project(nova-map-service C)

set(MAP_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../drive-os/hmi_lvgl/src/map")
if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
  set(DOS_MLN_PRESET linux-gnu-arm64-egl)
else()
  set(DOS_MLN_PRESET linux-gnu-x64-egl)
endif()
set(DOS_MAPLIBRE_PREFIX "${CMAKE_CURRENT_SOURCE_DIR}/../../drive-os/hmi_lvgl/.deps/maplibre/${DOS_MLN_PRESET}/maplibre-native-c-${DOS_MLN_PRESET}"
    CACHE PATH "MapLibre Native C package")

add_executable(nova-map-service main.c)
target_compile_options(nova-map-service PRIVATE -Wall -Wextra)

# DriveOS map sources, included by relative path. Not copied.
set(DOS_MAP_SOURCES
  ${MAP_DIR}/map_engine.c ${MAP_DIR}/map_worker.c ${MAP_DIR}/map_follow.c ${MAP_DIR}/map_puck.c
  ${MAP_DIR}/route_geojson.c ${MAP_DIR}/polyline.c ${MAP_DIR}/route_progress.c
  ${MAP_DIR}/maneuver_arrow.c ${MAP_DIR}/lane_layout.c ${MAP_DIR}/map_style.c ${MAP_DIR}/mapbox_url.c)
if(EXISTS "${DOS_MAPLIBRE_PREFIX}/include/maplibre_native_c.h")
  message(STATUS "MapLibre package found at ${DOS_MAPLIBRE_PREFIX}. Building libnova_map.")
  find_package(PkgConfig REQUIRED)
  if(NOT TARGET PkgConfig::EGL)
    pkg_check_modules(EGL REQUIRED IMPORTED_TARGET egl)
  endif()
  find_package(Threads REQUIRED)
  add_library(nova_map SHARED nova_map.c
    ${MAP_DIR}/map_engine.c ${MAP_DIR}/map_worker.c ${MAP_DIR}/map_follow.c ${MAP_DIR}/map_puck.c
    ${MAP_DIR}/mapbox_url.c)
  set_target_properties(nova_map PROPERTIES C_STANDARD 23 C_EXTENSIONS ON OUTPUT_NAME nova_map)
  target_include_directories(nova_map PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../drive-os/hmi_lvgl/src"
    "${DOS_MAPLIBRE_PREFIX}/include")
  target_link_libraries(nova_map PRIVATE
    "${DOS_MAPLIBRE_PREFIX}/lib/libmaplibre-native-c.so"
    "${DOS_MAPLIBRE_PREFIX}/lib/libmln_ffi_platform.a"
    dl PkgConfig::EGL Threads::Threads m)
  target_compile_options(nova_map PRIVATE -Wall -Wextra)
  set_target_properties(nova_map PROPERTIES
    BUILD_RPATH "${DOS_MAPLIBRE_PREFIX}/lib"
    INSTALL_RPATH "${DOS_MAPLIBRE_PREFIX}/lib")
else()
  message(STATUS "MapLibre package not found at ${DOS_MAPLIBRE_PREFIX}; the Qt app keeps the still map")
endif()
