cmake_minimum_required(VERSION 3.20)
project(niugit_entry C)

add_executable(git launch-git.c)
if(MSVC)
  # Unicode entry (wmain), static CRT: the entry must run with zero deps.
  target_compile_options(git PRIVATE /W4 /MT)
  target_compile_definitions(git PRIVATE UNICODE _UNICODE)
  set_target_properties(git PROPERTIES LINK_FLAGS "/ENTRY:wmainCRTStartup")
endif()
