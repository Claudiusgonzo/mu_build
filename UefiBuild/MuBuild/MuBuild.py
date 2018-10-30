## @file MuBuild.py
# This module contains code that supports Project Mu CI/CD
# This is the main entry for the build and test process
# of Non-Product builds
#
##
# Copyright (c) 2018, Microsoft Corporation
#
# All rights reserved.
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
##
import os
import sys
import logging
import yaml
import traceback
import argparse

#get path to self and then find SDE path and PythonLibrary path
SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__)) 
SDE_PATH = os.path.dirname(SCRIPT_PATH) #Path to SDE build env
PL_PATH = os.path.join(os.path.dirname(SDE_PATH), "BaseTools", "PythonLibrary")
sys.path.append(SDE_PATH)
sys.path.append(PL_PATH)

import SelfDescribingEnvironment
import PluginManager
from MuJunitReport import MuJunitReport
import CommonBuildEntry
import ShellEnvironment
import MuLogging
from Uefi.EdkII.PathUtilities import Edk2Path
import RepoResolver
import ConfigValidator


PROJECT_SCOPES = ("project_mu",)
TEMP_MODULE_DIR = "temp_modules"

def get_mu_config():
    parser = argparse.ArgumentParser(description='Run the Mu Build')
    parser.add_argument ('-c', '--mu_config', dest = 'mu_config', required = True, type=str, help ='Provide the Mu config relative to the current working directory')
    parser.add_argument ('-p', '--pkg','--pkg-dir', dest='pkglist', nargs="+", type=str, help = 'A package or folder you want to test (abs path or cwd relative).  Can list multiple by doing -p <pkg1> <pkg2> <pkg3>', default=[])
    args, sys.argv = parser.parse_known_args() 
    return args

def merge_config(mu_config,pkg_config,descriptor={}):
    plugin_name = ""
    config = dict()
    if "module" in descriptor:
        plugin_name = descriptor["module"]
    if "config_name" in descriptor:
        plugin_name = descriptor["config_name"]
    
    if plugin_name == "":
        return config

    if plugin_name in mu_config:
        config.update(mu_config[plugin_name])
    
    if plugin_name in pkg_config:
        config.update(pkg_config[plugin_name])

    return config

#
# Main driver of Project Mu Builds
#
if __name__ == '__main__':

    #Parse command line arguments
    buildArgs = get_mu_config()
    mu_config_filepath = os.path.abspath(buildArgs.mu_config)
    
    if mu_config_filepath is None or not os.path.isfile(mu_config_filepath):
        raise Exception("Invalid path to mu.json file for build: ", mu_config_filepath)
    
    #have a build config file
    mu_config = yaml.load(mu_config_filepath)
    WORKSPACE_PATH = os.path.realpath(os.path.join(os.path.dirname(mu_config_filepath), mu_config["RelativeWorkspaceRoot"]))

    #Setup the logging to the file as well as the console
    MuLogging.clean_build_logs(WORKSPACE_PATH)
    MuLogging.setup_logging(WORKSPACE_PATH)

    #Get scopes from config file
    if "Scopes" in mu_config:
        PROJECT_SCOPES += tuple(mu_config["Scopes"])

    # SET PACKAGE PATH
    #     
    # Get Package Path from config file
    pplist = list()
    if(mu_config["RelativeWorkspaceRoot"] != ""):
        #this package is not at workspace root. 
        # Add self
        pplist.append(os.path.dirname(mu_config_filepath))
    
    #Include packages from the config file
    if "PackagesPath" in mu_config:
        for a in mu_config["PackagesPath"]:
            pplist.append(a)

    #Check Dependencies for Repo
    if "Dependencies" in mu_config:
        pplist.extend(RepoResolver.resolve(WORKSPACE_PATH,mu_config["Dependencies"]))



    #make Edk2Path object to handle all path operations 
    edk2path = Edk2Path(WORKSPACE_PATH, pplist)

    logging.info("Running ProjectMu Build: {0}".format(mu_config["Name"]))
    logging.info("WorkSpace: {0}".format(edk2path.WorkspacePath))
    logging.info("Package Path: {0}".format(edk2path.PackagePathList))
    
    #which package to build
    packageList = mu_config["Packages"]
    #
    # If mu pk list supplied lets see if they are a file system path
    # If so convert to edk2 relative path
    #
    #
    if(len(buildArgs.pkglist) > 0):
        packageList = []  #clear it

    for mu_pk_path in buildArgs.pkglist:
        #if abs path lets convert
        if os.path.isabs(mu_pk_path):
            temp = edk2path.GetEdk2RelativePathFromAbsolutePath(mu_pk_path)
            if(temp is not None):
                packageList.append(temp)
            else:
                logging.critical("pkg-dir invalid absolute path: {0}".format(mu_pk_path))
                raise Exception("Invalid Package Path")
        else: 
            #Check if relative path
            temp = os.path.join(os.getcwd(), mu_pk_path)
            temp = edk2path.GetEdk2RelativePathFromAbsolutePath(temp)
            if(temp is not None):
                packageList.append(temp)
            else:
                logging.critical("pkg-dir invalid relative path: {0}".format(mu_pk_path))
                raise Exception("Invalid Package Path")
    
    # Bring up the common minimum environment.
    (build_env, shell_env) = SelfDescribingEnvironment.BootstrapEnvironment(edk2path.WorkspacePath, PROJECT_SCOPES)
    CommonBuildEntry.update_process(edk2path.WorkspacePath, PROJECT_SCOPES)
    env = ShellEnvironment.GetBuildVars()

    
    archSupported = " ".join(mu_config["ArchSupported"])
    env.SetValue("TARGET_ARCH", archSupported, "Platform Hardcoded")
    
    
    #Generate consumable XML object- junit format
    JunitReport = MuJunitReport()

    #Keep track of failures
    failure_num = 0
    total_num = 0

    #Load plugins
    pluginManager = PluginManager.PluginManager()
    pluginManager.SetListOfEnvironmentDescriptors(build_env.plugins)
    helper = PluginManager.HelperFunctions()
    helper.LoadFromPluginManager(pluginManager)
    pluginList = pluginManager.GetPluginsOfClass(PluginManager.IMuBuildPlugin)
    
    # Check to make sure our configuration is valid
    ConfigValidator.check_mu_confg(mu_config,edk2path,pluginList)

    for pkgToRunOn in packageList:
        #
        # run all loaded MuBuild Plugins/Tests
        #
        ts = JunitReport.create_new_testsuite(pkgToRunOn, "MuBuild.{0}.{1}".format( mu_config["GroupName"], pkgToRunOn) )
        _, loghandle = MuLogging.setup_logging(WORKSPACE_PATH,"BUILDLOG_{0}.txt".format(pkgToRunOn))
        logging.info("Package Running: {0}".format(pkgToRunOn))
        ShellEnvironment.CheckpointBuildVars()
        env = ShellEnvironment.GetBuildVars()

        # load the package level .mu.json
        pkg_config_file = edk2path.GetAbsolutePathOnThisSytemFromEdk2RelativePath(os.path.join(pkgToRunOn, pkgToRunOn + ".mu.json"))
        if(pkg_config_file):
            pkg_config = yaml.load(pkg_config_file)
        else:
            logging.info("No Pkg Config file for {0}".format(pkgToRunOn))
            pkg_config = dict()

        #check the resulting configuration
        ConfigValidator.check_package_confg(pkgToRunOn,pkg_config,pluginList)

        for Descriptor in pluginList:
            #Get our targets
            targets = ["DEBUG"]
            if Descriptor.Obj.IsTargetDependent() and "Targets" in mu_config:
                targets = mu_config["Targets"]
            
            
            for target in targets:
                logging.critical("---Running {2}: {0} {1}".format(Descriptor.Name,target,pkgToRunOn))
                total_num +=1
                ShellEnvironment.CheckpointBuildVars()
                env = ShellEnvironment.GetBuildVars()
            
                env.SetValue("TARGET", target, "MuBuild.py before RunBuildPlugin")
                (testcasename, testclassname) = Descriptor.Obj.GetTestName(pkgToRunOn, env)
                tc = ts.create_new_testcase(testcasename, testclassname)

                #merge the repo level and package level for this specific plugin
                pkg_plugin_configuration = merge_config(mu_config,pkg_config,Descriptor.descriptor)

                #perhaps we should ask the validator to run on the 

                #Check if need to skip this particular plugin
                if "skip" in pkg_plugin_configuration and pkg_plugin_configuration["skip"]:
                    tc.SetSkipped()
                    logging.critical("  ->Test Skipped! %s" % Descriptor.Name)
                else:
                    try:
                        #   - package is the edk2 path to package.  This means workspace/packagepath relative.  
                        #   - edk2path object configured with workspace and packages path
                        #   - any additional command line args
                        #   - RepoConfig Object (dict) for the build
                        #   - PkgConfig Object (dict)
                        #   - EnvConfig Object 
                        #   - Plugin Manager Instance
                        #   - Plugin Helper Obj Instance
                        #   - testcase Object used for outputing junit results
                        rc = Descriptor.Obj.RunBuildPlugin(pkgToRunOn, edk2path, sys.argv, mu_config, pkg_plugin_configuration, env, pluginManager, helper, tc)
                    except Exception as exp:
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        logging.critical("EXCEPTION: {0}".format(exp))
                        exceptionPrint = traceback.format_exception(type(exp), exp,exc_traceback)
                        logging.critical(" ".join(exceptionPrint))
                        tc.SetError("Exception: {0}".format(exp), "UNEXPECTED EXCEPTION")
                        rc = 1
                        

                    if(rc != 0):
                        failure_num += 1
                        if(rc is None):
                            logging.error("Test Failed: %s returned NoneType" % Descriptor.Name)
                        else:
                            logging.error("Test Failed: %s returned %d" % (Descriptor.Name, rc))
                    else:
                        logging.info("Test Success {0} {1}".format(Descriptor.Name,target))
           
                #revert to the checkpoint we created previously
                ShellEnvironment.RevertBuildVars()
            #finished target loop
        #Finished plugin loop
        
        MuLogging.stop_logging(loghandle) #stop the logging for this particularbuild file
        ShellEnvironment.RevertBuildVars()
    #Finished buildable file loop


    JunitReport.Output(os.path.join(WORKSPACE_PATH, "Build", "BuildLogs", "TestSuites.xml"))

      #Print Overall Success
    if(failure_num != 0):
        logging.critical("Overall Build Status: Error")
        logging.critical("There were {0} failures out of {1} attempts".format(failure_num,total_num))        
    else:
        logging.critical("Overall Build Status: Success")

    sys.exit(failure_num)
    