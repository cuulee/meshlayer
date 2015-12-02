# -*- coding: utf-8 -*-

from OpenGL.GL import *
from OpenGL.GL import shaders

from PyQt4.QtOpenGL import QGLPixelBuffer, QGLFormat

from PyQt4.QtCore import *
from PyQt4.QtGui import *

from qgis.core import *

import numpy
from math import pow, log, ceil

import os
import re
import time
import traceback

from glmesh import GlMesh, ColorLegend

from meshdataproviderregistry import MeshDataProviderRegistry
from meshlayerpropertydialog import MeshLayerPropertyDialog

from utilities import linemerge

class MeshLayerType(QgsPluginLayerType):
    def __init__(self):
        QgsPluginLayerType.__init__(self, MeshLayer.LAYER_TYPE)
        self.__dlg = None

    def createLayer(self):
        return MeshLayer()

        # indicate that we have shown the properties dialog
        return True

    def showLayerProperties(self, layer):
        self.__dlg = MeshLayerPropertyDialog(layer)
        return True


class MeshLayerLegendNode(QgsLayerTreeModelLegendNode):
    def __init__(self, nodeLayer, parent, legend):
        QgsLayerTreeModelLegendNode.__init__(self, nodeLayer, parent)
        self.text = ""
        self.__legend = legend

    def data(self, role):
        if role == Qt.DisplayRole or role == Qt.EditRole:
            return self.text
        elif role  == Qt.DecorationRole:
            return self.__legend.image()
        else:
            return None
    
    def draw(self, settings, ctx):
        symbolLabelFont = settings.style(QgsComposerLegendStyle.SymbolLabel).font()
        textHeight = settings.fontHeightCharacterMM(symbolLabelFont, '0');

        im = QgsLayerTreeModelLegendNode.ItemMetrics()
        context = QgsRenderContext()
        context.setScaleFactor( settings.dpi() / 25.4 )
        context.setRendererScale( settings.mapScale() )
        context.setMapToPixel( QgsMapToPixel( 1 / ( settings.mmPerMapUnit() * context.scaleFactor() ) ) )

        sz = self.__legend.sceneRect().size()
        aspect = sz.width() / sz.height()
        h = textHeight*16
        w = aspect*h
        im.symbolSize = QSizeF(w, h)
        im.labeSize =  QSizeF(0, 0)
        if ctx:
            currentXPosition = ctx.point.x()
            currentYCoord = ctx.point.y() #\
                    #+ settings.symbolSize().height()/2;
            ctx.painter.save()
            ctx.painter.translate(currentXPosition, currentYCoord)
            rect = QRectF()
            rect.setSize(QSizeF(im.symbolSize))
            self.__legend.render(ctx.painter, rect) 
            #ctx.painter.drawImage(0, 0, self.image)
            ctx.painter.restore()
        return im

class MeshLayerLegend(QgsDefaultPluginLayerLegend):
    def __init__(self, layer, legend):
        QgsDefaultPluginLayerLegend.__init__(self, layer)
        self.nodes = []
        self.__legend = legend

    def createLayerTreeModelLegendNodes(self, nodeLayer):
        node = MeshLayerLegendNode(nodeLayer, self, self.__legend)
        self.nodes = [node]
        return self.nodes

class MeshLayer(QgsPluginLayer):

    LAYER_TYPE="mesh_layer"

    __msg = pyqtSignal(str)
    __drawException = pyqtSignal(str)
    __imageChangeRequested = pyqtSignal()

    def __print(self, msg):
        print msg

    def __raise(self, err):
        raise Exception(err)

    def __init__(self, uri=None, name=None, providerKey=None):
        """optional parameters are here only in the case the layer is created from
        .gqs file, without them the layer is invalid"""
        QgsPluginLayer.__init__(self, MeshLayer.LAYER_TYPE, name)
        self.__meshDataProvider = None
        self.__legend = None
        self.__imageChangedMutex = QMutex()
        self.__imageChangeRequested.connect(self.__drawInMainThread)
        self.__img = None

        if uri:
            self.__load(MeshDataProviderRegistry.instance().provider(providerKey, uri))
        self.__drawException.connect(self.__raise)
        self.__msg.connect(self.__print)
        self.__destCRS = None

    def setColorLegend(self, legend):
        if self.__legend:
            self.__legend.symbologyChanged.disconnect(self.__symbologyChanged)
        self.__legend = legend
        self.__glMesh.setLegend(self.__legend)
        self.__legend.symbologyChanged.connect(self.__symbologyChanged)

    def colorLegend(self):
        return self.__legend

    def __load(self, meshDataProvider):
        self.setCrs(meshDataProvider.crs())
        self.setExtent(meshDataProvider.extent())
        self.__meshDataProvider = meshDataProvider
        self.__meshDataProvider.dataChanged.connect(self.triggerRepaint)

        self.__legend = ColorLegend()
        self.__legend.setParent(self)
        self.__legend.symbologyChanged.connect(self.__symbologyChanged)
        self.__glMesh = GlMesh(
                meshDataProvider.nodeCoord(), 
                meshDataProvider.triangles(),
                self.__legend
                )
        self.setValid(self.__meshDataProvider.isValid())
        self.__symbologyChanged()

    def __symbologyChanged(self):
        self.__layerLegend = MeshLayerLegend(self, self.__legend)
        self.setLegend(self.__layerLegend)
        self.legendChanged.emit()
        self.triggerRepaint()

    def readXml(self, node):
        element = node.toElement()
        provider = node.namedItem("meshDataProvider").toElement()
        meshDataProvider = MeshDataProviderRegistry.instance().provider(
                provider.attribute("name"), provider.attribute("uri"))
        if not meshDataProvider.readXml(node.namedItem("meshDataProvider")):
            return False

        self.__load(meshDataProvider)

        if not self.__legend.readXml(node.namedItem("colorLegend")):
            return False
        return True

    def writeXml(self, node, doc):
        """write plugin layer type to project (essential to be read from project)"""
        element = node.toElement()
        element.setAttribute("debug", "just a test")
        element.setAttribute("type", "plugin")
        element.setAttribute("name", MeshLayer.LAYER_TYPE)

        dataProvider = doc.createElement("meshDataProvider")
        if not self.__meshDataProvider.writeXml(dataProvider, doc):
            return False
        element.appendChild(dataProvider)
        
        colorLegend = doc.createElement("colorLegend")
        if not self.__legend.writeXml(colorLegend, doc):
            return False
        element.appendChild(colorLegend)
        return True

    def dataProvider(self):
        return self.__meshDataProvider

    def __drawInMainThread(self):
        self.__imageChangedMutex.lock()

        if self.__transform \
                and self.__transform.destCRS() != self.__destCRS:
            self.__destCRS = self.__transform.destCRS()
            vtx = numpy.array(self.__meshDataProvider.nodeCoord())
            def transf(x):
                p = self.__transform.transform(x[0], x[1])
                return [p.x(), p.y(), x[2]]
            vtx = numpy.apply_along_axis(transf, 1, vtx)
            self.__glMesh.resetCoord(vtx)

        if self.__transform:
            self.__ext = self.__transform.transform(self.__ext)
        self.__glMesh.setColorPerElement(self.__meshDataProvider.valueAtElement())
        self.__img = self.__glMesh.image(
                self.__meshDataProvider.elementValues() 
                   if self.__meshDataProvider.valueAtElement() else
                   self.__meshDataProvider.nodeValues(),
                self.__windowSize,
                (.5*(self.__ext.xMinimum() + self.__ext.xMaximum()), 
                 .5*(self.__ext.yMinimum() + self.__ext.yMaximum())),
                (self.__mapToPixel.mapUnitsPerPixel(), 
                 self.__mapToPixel.mapUnitsPerPixel()),
                 self.__mapToPixel.mapRotation())
        self.__imageChangedMutex.unlock()

    def draw(self, rendererContext):
        """This function is called by the rendering thread. 
        GlMesh must be created in the main thread."""
        try:
            painter = rendererContext.painter()
            if QApplication.instance().thread() != QThread.currentThread():
                self.__imageChangedMutex.lock()
                self.__transform = rendererContext.coordinateTransform()
                self.__windowSize = rendererContext.painter().window().size()
                self.__ext = rendererContext.extent()
                self.__mapToPixel = rendererContext.mapToPixel()
                self.__img = None
                self.__imageChangedMutex.unlock()
                self.__imageChangeRequested.emit()
                while not self.__img and not rendererContext.renderingStopped():
                    time.sleep(.001) # active wait to avoid deadlocking if envent loop is stopped
                                     # this happens when a render job is cancellled
                if not rendererContext.renderingStopped():
                    painter.drawImage(painter.window(), self.__img)
            else:
                self.__imageChangedMutex.lock()
                self.__transform = rendererContext.coordinateTransform()
                self.__windowSize = rendererContext.painter().window().size()
                self.__ext = rendererContext.extent()
                self.__mapToPixel = rendererContext.mapToPixel()
                self.__imageChangedMutex.unlock()
                self.__drawInMainThread()
                painter.drawImage(painter.window(), self.__img)

            return True
        except Exception as e:
            # since we are in a thread, we must re-raise the exception
            self.__drawException.emit(traceback.format_exc())
            return False

    def isovalues(self, values):
        """return a list of multilinestring, one for each value in values"""
        idx = self.__meshDataProvider.triangles()
        vtx = self.__meshDataProvider.nodeCoord()
        lines = []
        for value in values:
            lines.append([])
            val = self.__meshDataProvider.nodeValues() - float(value)
            # we filer triangles in which the value is negative on at least
            # one node and positive on at leat one node
            filtered = idx[numpy.logical_or(
                val[idx[:, 0]]*val[idx[:, 1]] <= 0, 
                val[idx[:, 0]]*val[idx[:, 2]] <= 0 
                ).reshape((-1,))]
            # create line segments
            for tri in filtered:
                line = []
                # the edges are sorted to avoid interpolation error
                for edge in [sorted([tri[0], tri[1]]), 
                             sorted([tri[1], tri[2]]), 
                             sorted([tri[2], tri[0]])]:
                    if val[edge[0]]*val[edge[1]] <= 0:
                        alpha = -val[edge[0]]/(val[edge[1]] - val[edge[0]])\
                                if val[edge[1]] != val[edge[0]]\
                                else None
                        if alpha: # the isoline crosses the edge
                            assert alpha >= 0 and alpha <= 1
                            line.append( (1-alpha)*vtx[edge[0]] + alpha*vtx[edge[1]])
                        else: # the edge is part of the isoline
                            line.append(vtx[edge[0]])
                            line.append(vtx[edge[1]])
                if numpy.any(line[0] != line[-1]):
                    lines[-1].append(line)
            lines[-1] = linemerge(lines[-1])
        return lines


if __name__ == "__main__":
    import sys
    from winddataprovider import WindDataProvider

    #app = QgsApplication(sys.argv, False)
    QgsApplication.setPrefixPath('/usr/local', True)
    QgsApplication.initQgis()

    MeshDataProviderRegistry.instance().addDataProviderType("wind", WindDataProvider)

    assert len(sys.argv) >= 2
    


    uri = 'directory='+sys.argv[1]+' crs=epsg:2154'
    provider = MeshDataProviderRegistry.instance().provider("wind", uri)
    print provider
    print provider.crs()
    print provider.isValid()
    print "####################"




    layer = MeshLayer(uri, 'test_layer', "wind")
    layer.dataProvider().setDate(int(sys.argv[2]))
    print layer.dataProvider().dataSourceUri()
    print layer.dataProvider().nodeValues()

    exit(0)
    # the rest should be ported to a specific test


    start = time.time()
    values = [float(v) for v in sys.argv[4:]]
    lines = layer.isovalues(values)
    print "total time ", time.time() - start 
    isolines = QgsVectorLayer("MultiLineString?crs=epsg:27572&field=value:double", "isovalues", "memory")
    pr = isolines.dataProvider()
    features = []
    for i, mutilineline in enumerate(lines):
        features.append(QgsFeature())
        features[-1].setGeometry(QgsGeometry.fromMultiPolyline([[QgsPoint(point[0], point[1]) \
                for point in line] for line in mutilineline]))
        features[-1].setAttributes([values[i]])
    pr.addFeatures(features)

    QgsVectorFileWriter.writeAsVectorFormat(isolines, "isovalues.shp", "isovalues", None, "ESRI Shapefile")



