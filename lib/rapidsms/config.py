#!/usr/bin/env python
# vim: ai ts=4 sts=4 et sw=4

import os, log
from ConfigParser import SafeConfigParser
import logging

def to_list (item, separator=","):
    return filter(None, map(lambda x: str(x).strip(), item.split(separator)))


class LazyAppConf(object):
    """RapidSMS apps can provide an optional config.py, which is like a per-app
       version of Django's settings.py. The settings defined there are available
       to both the RapidSMS router and the Django WebUI. This class acts very
       much like a dict, but doesn't build the app config until it's really
       required (much like a Django QuerySet).

       This is almost completely useless, except it allows us to prepare the
       Config object (full of LazyAppConf instances) while building the Django
       settings (rapidsms.webui.settings), _and_ allow app configs to hit the
       database. If the configs were regular (eager) dicts, Django would refuse
       to hit the database, because "You haven't set the DATABASE_ENGINE setting
       yet".
       
       Unfortunately, for Python2.5 compatibility, this must be cast to a real
       dict before being iterated or passed as **kwargs."""

    def __init__(self, config, app_name):
        self.app_name = app_name
        self.config = config
        self._cache = None

    def _dict(self):
        if self._cache is None:
            self._cache = self.config.app_section(self.app_name)
        return self._cache

    # not strictly necessary, but avoids logging the useless
    # "Added app: <rapidsms.config.LazyAppConf object at 0x......c>"
    def __repr__(self):
        return repr(self._dict())

    # these two methods are enough to cast to a dict in
    # python 2.5. in python 2.6, use collections.mapping.
    def keys(self):
        return self._dict().keys()

    def __getitem__(self, key):
        return self._dict()[key]


class Config (object):
    app_prefixes = ["rapidsms.contrib.apps", "rapidsms.apps"]
    
    
    def __init__ (self, *paths):
        self.parser = SafeConfigParser()

        # read the configuration, and store the list of
        # config files which were successfully parsed
        self.sources = self.parser.read(paths)
        
        self.raw_data = {}
        self.normalized_data = {}
        self.data = {}
        
        # first pass: read in the raw data. it's all strings, since
        # ConfigParser doesn't seem to decode unicode correctly (yet)
        for sn in self.parser.sections():
            items = self.parser.items(sn)
            self.raw_data[sn] = dict(items)
        
        # second pass: cast the values into int or bool where possible
        # (mostly to avoid storing "false", which evaluates to True)
        for sn in self.raw_data.keys():
            self.normalized_data[sn] = {}
            
            for key, val in self.raw_data[sn].items():
                self.normalized_data[sn][key] = \
                    self.__normalize_value(val)
        
        # third pass: iterate the normalized data, creating a
        # dict (self.data) containing the "real" configuration,
        # which may include things (magic, defaults, etc) not
        # present in the raw_data or normalized_data
        for sn in self.normalized_data.keys():
            section_parser = "parse_%s_section" % (sn)
            
            # if this section has a special parser, call
            # it with the raw data, and store the result
            if hasattr(self, section_parser):
                self.data[sn] = \
                    getattr(self, section_parser)(
                        self.normalized_data[sn])
            
            # no custom section parser, so
            # just copy the raw data as-is
            else:
                self.data[sn] =\
                    self.normalized_data[sn].copy()


    def __normalize_value (self, value):
        """Casts a string to a bool, int, or float, if it looks like it
           should be one. This is a band-aid over the ini format, which
           assumes all values to be strings. Examples:
           
           "mudkips"              => "mudkips" (str)
           "false", "FALSE", "no" => False     (bool)
           "true", "TRUE", "yes"  => True      (bool)
           "1.0", "0001.00"       => 1.0       (float)
           "0", "0000"            => 0         (int)"""
        
        # shortcut for string boolean values
        if   value.lower() in ["false", "no"]: return False
        elif value.lower() in ["true", "yes"]: return True
        
        # attempt to cast this value to an int, then a float. (a sloppy
        # benchmark of this exception-catching algorithm indicates that
        # it's faster than checking with a regexp)
        for func in [int, float]:
            try: func(value)
            except: pass
        
        # it's just a str
        # (NOT A UNICODE)
        return value
    
    
    def __import_class (self, class_tmpl):
        """Given a full class name (ie, webapp.app.App), returns the
           class object. There doesn't seem to be a built-in way of doing
           this without mucking with __import__."""
        
        # break the class name off the end of  module template
        # i.e. "ABCD.app.App" -> ("ABC.app", "App")
        try:
            split_module = class_tmpl.rsplit(".",1)            
            module = __import__(split_module[0], {}, {}, split_module[1:])
            #module = __import__(class_tmpl, {}, {}, [])
            
            # import the requested class or None
            if len(split_module) > 1 and hasattr(module, split_module[-1]):
                return getattr(module, split_module[-1])
            else:
                return module
        
        except ImportError, e:
            logging.error("App import error: " + str(e))            
            pass


    def __import_app (self, app_type):
        """Iterates the modules in which RapidSMS apps can live (apps,
           rapidsms.contrib.apps, and rapidsms.apps), attempting to load
           _app_type_ from each. When an app is found, returns a tuple
           containing the full module name and the module itself. If no
           module is found, raises ImportError."""
           
        try:
            module = self.__import_class(app_type)
            return app_type, module

        except ImportError:
            pass

        # iterate the places that apps might live,
        # and attempt to import app_type from each
        for prefix in self.app_prefixes:
            mod_str = ".".join([prefix, app_type])
            module = self.__import_class(mod_str)
            
            # we found the app! return it!
            if module is not None:
                return mod_str, module

        # the module couldn't be imported. it's probably a
        # typo in the ini, or a missing app directory. either
        # way, explode, because this app may be necessary to
        # run properly (especially during ./rapidsms syncdb)
        raise ImportError(
            'Couldn\'t import "%s" from %s' %
                (app_type, " or ".join(self.app_prefixes)))


    def lazy_app_section (self, name):
        return LazyAppConf(self, name)


    def app_section (self, name):

        # fetch the current config for this app
        # from raw_data (or default to an empty dict),
        # then copy it, so we don't alter the original
        data = self.raw_data.get(name, {}).copy()

        # "type" is ONLY VALID FOR BACKENDS now. it's not [easily] possible
        # to run multple django apps of the same type side-by-side, so i'm
        # warning here to avoid confusion (and allow apps to be lazy loaded)
        if "type" in data:
            raise DeprecationWarning(
                'The "type" option is not supported for apps. It does ' +\
                'nothing, since running multple apps of the same type ' +\
                'is not currently possible.')

        # ...that said, the terms _type_ and _name_ are still mixed up
        # in various places, so we must support both. another naming
        # upheaval is probably needed to clear this up (or perhaps we
        # should just scrap the shitty INI format, like we should have
        # done in the first place to avoid this entire mess)
        data["type"] = name

        try:
            # attempt to import the module, to locate it (it might be in ./apps,
            # contrib, or rapidsms/apps) and verify that it imports cleanly
            data["module"], module = self.__import_app(data["type"])
            
            # load the config.py for this app, if possible
            config = self.__import_class("%s.config" % data["module"])
            if config is not None:

                # copy all of the names not starting with underscore (those are
                # private or __magic__) into this component's default config
                for var_name in dir(config):
                    if not var_name.startswith("_"):
                        data[var_name] = getattr(config, var_name)
            
            # the module was imported! add it's full path to the
            # config, since it might not be in rapidsms/apps/%s
            data["path"] = module.__path__[0]
            
            # return the component with the additional
            # app-specific data included.
            return data

        except Exception, e:
            print(e)


    def backend_section (self, name):

        # fetch the current config for this backend
        # from raw_data (or default to an empty dict),
        # then copy it, so we don't alter the original
        data = self.raw_data.get(name, {}).copy()

        # although "name" and "type" are deliberately distinct (to enable multiple
        # backends of the same type to run concurrently), it's cumbersome to have
        # to provide a type every single time, so default to the name
        if not "type" in data:
            data["type"] = name

        return data
    
    
    def parse_rapidsms_section (self, raw_section):
        
        # "apps" and "backends" are strings of comma-separated
        # component names. first, break them into real lists
        app_names     = to_list(raw_section.get("apps",     ""))
        backend_names = to_list(raw_section.get("backends", ""))
        
        # run lists of component names through [app|backend]_section,
        # to transform into dicts of dicts containing more meta-info
        return { "apps":     dict((n, self.lazy_app_section(n)) for n in app_names),
                 "backends": dict((n, self.backend_section(n)) for n in backend_names) }


    def parse_log_section (self, raw_section):
        output = {"level": log.LOG_LEVEL, "file": log.LOG_FILE}
        output.update(raw_section)
        return output

    def parse_i18n_section (self, raw_section):
        output = {}
        if "default_language" in raw_section:
            output.update( {"default_language" : raw_section["default_language"]} )
        
        def _add_language_settings(setting):
            if setting not in raw_section: return
            output.update( {setting:[]} )
            all_language_settings = to_list(raw_section[setting], separator="),(")
            for language_settings in all_language_settings:
                language = to_list( language_settings.strip('()') )
                output[setting].append( language )
        
        _add_language_settings("languages")
        _add_language_settings("web_languages")
        _add_language_settings("sms_languages")
        # add a section for the locale paths
        if "locale_paths" in raw_section:
            output["locale_paths"] = to_list(raw_section["locale_paths"], ",")
        
        return output

    def __getitem__ (self, key):
        return self.data[key]
        
    def has_key (self, key):
        return self.data.has_key(key)
    
    __contains__ = has_key
