cmake_minimum_required(VERSION 3.22.1)
project(elysium_oboe LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Oboe 通过 Google Maven 的 AAR 以 prefab 形式提供；Gradle 依赖见 app/build.gradle。
find_package(oboe REQUIRED CONFIG)

add_library(elysium_oboe SHARED oboe_karaoke.cpp)
target_link_libraries(elysium_oboe oboe::oboe android log)
