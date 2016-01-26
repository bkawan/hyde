# -*- coding: utf-8 -*-
"""
Sphinx plugin.

This plugin lets you easily include sphinx-generated documentation as part
of your Hyde site.  It is simultaneously a Hyde plugin and a Sphinx plugin.

To make this work, you need to:

    * install sphinx, obviously
    * include your sphinx source files in the Hyde source tree
    * put the sphinx conf.py file in the Hyde site directory
    * point conf.py:master_doc at an appropriate file in the source tree

For example you might have your site set up like this::

    site.yaml    <--  hyde config file
    conf.py      <--  sphinx config file
    contents/
        index.html     <-- non-sphinx files, handled by hyde
        other.html
        api/
            index.rst      <-- files to processed by sphinx
            mymodule.rst

When the site is built, the .rst files will first be processed by sphinx
to generate a HTML docuent, which will then be passed through the normal
hyde templating workflow.  You would end up with::

    deploy/
        index.html     <-- files generated by hyde
        other.html
        api/
            index.html      <-- files generated by sphinx, then hyde
            mymodule.html

"""

#  We need absolute import so that we can import the main "sphinx"
#  module even though this module is also called "sphinx". Ugh.
from __future__ import absolute_import

import os
import json
import tempfile

from hyde._compat import execfile, iteritems
from hyde.plugin import Plugin
from hyde.model import Expando
from hyde.ext.plugins.meta import MetaPlugin as _MetaPlugin

from commando.util import getLoggerWithNullHandler
from fswrap import File, Folder

logger = getLoggerWithNullHandler('hyde.ext.plugins.sphinx')

try:
    import sphinx
    from sphinx.builders.html import JSONHTMLBuilder
except ImportError:
    logger.error("The sphinx plugin requires sphinx.")
    logger.error("`pip install -U sphinx` to get it.")
    raise


class SphinxPlugin(Plugin):

    """The plugin class for rendering sphinx-generated documentation."""

    def __init__(self, site):
        self.sphinx_build_dir = None
        self._sphinx_config = None
        super(SphinxPlugin, self).__init__(site)

    @property
    def plugin_name(self):
        """The name of the plugin, obivously."""
        return "sphinx"

    @property
    def settings(self):
        """Settings for this plugin.

        This property combines default settings with those specified in the
        site config to produce the final settings for this plugin.
        """
        settings = Expando({})
        settings.sanity_check = True
        settings.conf_path = "."
        settings.block_map = {}
        try:
            user_settings = getattr(self.site.config, self.plugin_name)
        except AttributeError:
            pass
        else:
            for name in dir(user_settings):
                if not name.startswith("_"):
                    setattr(settings, name, getattr(user_settings, name))
        return settings

    @property
    def sphinx_config(self):
        """Configuration options for sphinx.

        This is a lazily-generated property giving the options from the
        sphinx configuration file.  It's generated by actualy executing
        the config file, so don't do anything silly in there.
        """
        if self._sphinx_config is None:
            conf_path = self.settings.conf_path
            conf_path = self.site.sitepath.child_folder(conf_path)
            #  Sphinx always execs the config file in its parent dir.
            conf_file = conf_path.child("conf.py")
            self._sphinx_config = {"__file__": conf_file}
            curdir = os.getcwd()
            os.chdir(conf_path.path)
            try:
                execfile(conf_file, self._sphinx_config)
            finally:
                os.chdir(curdir)
        return self._sphinx_config

    def begin_site(self):
        """Event hook for when site processing begins.

        This hook checks that the site is correctly configured for building
        with sphinx, and adjusts any sphinx-controlled resources so that
        hyde will process them correctly.
        """
        settings = self.settings
        if settings.sanity_check:
            self._sanity_check()
        #  Find and adjust all the resource that will be handled by sphinx.
        #  We need to:
        #    * change the deploy name from .rst to .html
        #    * if a block_map is given, switch off default_block
        suffix = self.sphinx_config.get("source_suffix", ".rst")
        for resource in self.site.content.walk_resources():
            if resource.source_file.path.endswith(suffix):
                new_name = resource.source_file.name_without_extension + \
                    ".html"
                target_folder = File(resource.relative_deploy_path).parent
                resource.relative_deploy_path = target_folder.child(new_name)
                if settings.block_map:
                    resource.meta.default_block = None

    def begin_text_resource(self, resource, text):
        """Event hook for processing an individual resource.

        If the input resource is a sphinx input file, this method will replace
        replace the text of the file with the sphinx-generated documentation.

        Sphinx itself is run lazily the first time this method is called.
        This means that if no sphinx-related resources need updating, then
        we entirely avoid running sphinx.
        """
        suffix = self.sphinx_config.get("source_suffix", ".rst")
        if not resource.source_file.path.endswith(suffix):
            return text
        if self.sphinx_build_dir is None:
            self._run_sphinx()
        output = []
        settings = self.settings
        sphinx_output = self._get_sphinx_output(resource)
        #  If they're set up a block_map, use the specific blocks.
        #  Otherwise, output just the body for use by default_block.
        if not settings.block_map:
            output.append(sphinx_output["body"])
        else:
            for (nm, content) in iteritems(sphinx_output):
                try:
                    block = getattr(settings.block_map, nm)
                except AttributeError:
                    pass
                else:
                    output.append("{%% block %s %%}" % (block,))
                    output.append(content)
                    output.append("{% endblock %}")
        return "\n".join(output)

    def site_complete(self):
        """Event hook for when site processing ends.

        This simply cleans up any temorary build file.
        """
        if self.sphinx_build_dir is not None:
            self.sphinx_build_dir.delete()

    def _sanity_check(self):
        """Check the current site for sanity.

        This method checks that the site is propertly set up for building
        things with sphinx, e.g. it has a config file, a master document,
        the hyde sphinx extension is enabled, and so-on.
        """
        #  Check that the sphinx config file actually exists.
        try:
            sphinx_config = self.sphinx_config
        except EnvironmentError:
            logger.error("Could not read the sphinx config file.")
            conf_path = self.settings.conf_path
            conf_path = self.site.sitepath.child_folder(conf_path)
            conf_file = conf_path.child("conf.py")
            logger.error(
                "Please ensure %s is a valid sphinx config", conf_file)
            logger.error("or set sphinx.conf_path to the directory")
            logger.error("containing your sphinx conf.py")
            raise
        #  Check that the hyde_json extension is loaded
        extensions = sphinx_config.get("extensions", [])
        if "hyde.ext.plugins.sphinx" not in extensions:
            logger.error("The hyde_json sphinx extension is not configured.")
            logger.error("Please add 'hyde.ext.plugins.sphinx' to the list")
            logger.error("of extensions in your sphinx conf.py file.")
            logger.info(
                "(set sphinx.sanity_check=false to disable this check)")
            raise RuntimeError("sphinx is not configured correctly")
        #  Check that the master doc exists in the source tree.
        master_doc = sphinx_config.get("master_doc", "index")
        master_doc += sphinx_config.get("source_suffix", ".rst")
        master_doc = os.path.join(self.site.content.path, master_doc)
        if not os.path.exists(master_doc):
            logger.error("The sphinx master document doesn't exist.")
            logger.error("Please create the file %s", master_doc)
            logger.error("or change the 'master_doc' setting in your")
            logger.error("sphinx conf.py file.")
            logger.info(
                "(set sphinx.sanity_check=false to disable this check)")
            raise RuntimeError("sphinx is not configured correctly")
        #  Check that I am *before* the other plugins,
        #  with the possible exception of MetaPlugin
        for plugin in self.site.plugins:
            if plugin is self:
                break
            if not isinstance(plugin, _MetaPlugin):
                logger.error("The sphinx plugin is installed after the")
                logger.error("plugin %r.", plugin.__class__.__name__)
                logger.error("It's quite likely that this will break things.")
                logger.error("Please move the sphinx plugin to the top")
                logger.error("of the plugins list.")
                logger.info(
                    "(sphinx.sanity_check=false to disable this check)")
                raise RuntimeError("sphinx is not configured correctly")

    def _run_sphinx(self):
        """Run sphinx to generate the necessary output files.

        This method creates a temporary directory for sphinx's output, then
        run sphinx against the Hyde input directory.
        """
        logger.info("running sphinx")
        self.sphinx_build_dir = Folder(tempfile.mkdtemp())
        conf_path = self.site.sitepath.child_folder(self.settings.conf_path)
        sphinx_args = ["sphinx-build"]
        sphinx_args.extend([
            "-b", "hyde_json",
            "-c", conf_path.path,
            self.site.content.path,
            self.sphinx_build_dir.path
        ])
        if sphinx.main(sphinx_args) != 0:
            raise RuntimeError("sphinx build failed")

    def _get_sphinx_output(self, resource):
        """Get the sphinx output for a given resource.

        This returns a dict mapping block names to HTML text fragments.
        The most important fragment is "body" which contains the main text
        of the document.  The other fragments are for things like navigation,
        related pages and so-on.
        """
        relpath = File(resource.relative_path)
        relpath = relpath.parent.child(
            relpath.name_without_extension + ".fjson")
        with open(self.sphinx_build_dir.child(relpath), "rb") as f:
            return json.load(f)


class HydeJSONHTMLBuilder(JSONHTMLBuilder):

    """A slightly-customised JSONHTMLBuilder, for use by Hyde.

    This is a Sphinx builder that serilises the generated HTML fragments into
    a JSON docuent, so they can be later retrieved and dealt with at will.

    The only customistion we do over the standard JSONHTMLBuilder is to
    reference documents with a .html suffix, so that internal link will
    work correctly once things have been processed by Hyde.
    """
    name = "hyde_json"

    def get_target_uri(self, docname, typ=None):
        return docname + ".html"


def setup(app):
    """Sphinx plugin setup function.

    This function allows the module to act as a Sphinx plugin as well as a
    Hyde plugin.  It simply registers the HydeJSONHTMLBuilder class.
    """
    app.add_builder(HydeJSONHTMLBuilder)
