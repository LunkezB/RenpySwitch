cmake_minimum_required(VERSION 3.13)

# Ren'Py 8.5 for Nintendo Switch homebrew: one static executable containing
# CPython 3.14 (NXPython), Ren'Py's Cython modules and the launcher.
#
# Configure with the devkitPro toolchain and the generated module list:
#   cmake -S switch -B build-switch -DRENPY_GEN_OUT=<dir from switch_modules.py>

if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
    if(DEFINED ENV{DEVKITPRO})
        set(CMAKE_TOOLCHAIN_FILE "$ENV{DEVKITPRO}/cmake/Switch.cmake" CACHE PATH "toolchain file")
    else()
        message(FATAL_ERROR "Please define DEVKITPRO to point to your devkitPro install.")
    endif()
endif()

project(renpy-switch C CXX)

set(DEVKITPRO $ENV{DEVKITPRO})
set(PORTLIBS ${DEVKITPRO}/portlibs/switch)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

if(NOT RENPY_GEN_OUT)
    message(FATAL_ERROR "RENPY_GEN_OUT is not set (run scripts/switch_modules.py first).")
endif()

# RENPY_SOURCE_DIR, RENPY_GEN_DIR, RENPY_MODULE_SOURCES
include(${RENPY_GEN_OUT}/renpy_modules.cmake)

# ---------------------------------------------------------------------------
# Dependencies via the devkitPro pkg-config wrapper (static flags).

find_program(SWITCH_PKG_CONFIG NAMES aarch64-none-elf-pkg-config
    HINTS ${PORTLIBS}/bin NO_CMAKE_FIND_ROOT_PATH)
if(NOT SWITCH_PKG_CONFIG)
    message(FATAL_ERROR "aarch64-none-elf-pkg-config not found (install switch-pkg-config).")
endif()

set(RENPY_PKGS
    python-3.14-embed
    sdl2
    SDL2_image
    freetype2
    harfbuzz
    fribidi
    libavformat
    libavcodec
    libavutil
    libswresample
    libswscale
    libpng
    libjpeg
    libwebp
    zlib
    egl
    glesv2
    glapi
    libdrm_nouveau
)

execute_process(
    COMMAND ${SWITCH_PKG_CONFIG} --cflags ${RENPY_PKGS}
    OUTPUT_VARIABLE PKG_CFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE PKG_CFLAGS_RESULT)
execute_process(
    COMMAND ${SWITCH_PKG_CONFIG} --static --libs ${RENPY_PKGS}
    OUTPUT_VARIABLE PKG_LIBS OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE PKG_LIBS_RESULT)

if(NOT PKG_CFLAGS_RESULT EQUAL 0 OR NOT PKG_LIBS_RESULT EQUAL 0)
    # Report which package is missing.
    foreach(pkg ${RENPY_PKGS})
        execute_process(COMMAND ${SWITCH_PKG_CONFIG} --exists ${pkg} RESULT_VARIABLE r)
        if(NOT r EQUAL 0)
            message(SEND_ERROR "pkg-config: '${pkg}' not found for the Switch.")
        endif()
    endforeach()
    message(FATAL_ERROR "Missing Switch portlibs (see above).")
endif()

separate_arguments(PKG_CFLAGS_LIST UNIX_COMMAND "${PKG_CFLAGS}")
separate_arguments(PKG_LIBS_LIST UNIX_COMMAND "${PKG_LIBS}")

message(STATUS "Ren'Py sources: ${RENPY_SOURCE_DIR}")
message(STATUS "pkg-config libs: ${PKG_LIBS}")

# ---------------------------------------------------------------------------

add_executable(renpy-switch source/main.c ${RENPY_MODULE_SOURCES})

target_include_directories(renpy-switch PRIVATE
    ${RENPY_GEN_OUT}
    ${RENPY_SOURCE_DIR}/src
    ${RENPY_SOURCE_DIR}/src/pygame
    ${RENPY_GEN_DIR}
    ${PORTLIBS}/include
    ${PORTLIBS}/include/SDL2
)

target_compile_options(renpy-switch PRIVATE
    ${PKG_CFLAGS_LIST}
    $<$<COMPILE_LANGUAGE:C>:-std=gnu11>
    -O2
    -ffunction-sections
    -fno-omit-frame-pointer
    -fno-strict-aliasing
    -Wno-unused-function
    -Wno-unused-variable
    -Wno-incompatible-pointer-types
    -Wno-int-conversion
    -Wno-deprecated-declarations
)

target_compile_definitions(renpy-switch PRIVATE
    __SWITCH__
    RENPY_SWITCH_STACK_SIZE=33554432
)

target_link_options(renpy-switch PRIVATE
    -Wl,-z,notext
    -Wl,--gc-sections
)

target_link_libraries(renpy-switch PRIVATE
    -Wl,--start-group
    ${PKG_LIBS_LIST}
    stdc++
    nx
    m
    -Wl,--end-group
)

# ELF -> NSO (for an ExeFS / installable build) next to the ELF.
add_custom_command(TARGET renpy-switch POST_BUILD
    COMMAND ${DEVKITPRO}/tools/bin/elf2nso
        $<TARGET_FILE:renpy-switch>
        $<TARGET_FILE_DIR:renpy-switch>/renpy-switch.nso
    COMMENT "Creating renpy-switch.nso")
