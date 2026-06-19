cmake_minimum_required(VERSION 3.16)

# Add compile options
add_compile_options(-Wno-missing-field-initializers)

set(EXTRA_COMPONENT_DIRS
    ${CMAKE_SOURCE_DIR}/components
)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ai-cat)
