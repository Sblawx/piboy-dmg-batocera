#!/usr/bin/env python3

import sys
import time
import random
import serial
import struct
import time

class Utilities:
    'Utilities Interface Class'
    
    def __init__(self, comport, rate, timeout=0.05, retries=3):
        self.comport = comport
        self.rate = rate
        self.timeout = timeout;
        self._trystimeout = retries
        self._crc = 0;

    #Private Functions
    def crc_clear(self):
        self._crc = 0
        return
        
    def crc_update(self,data):
        self._crc = self._crc ^ (data << 8)
        for bit in range(0, 8):
            if (self._crc&0x8000)  == 0x8000:
                self._crc = ((self._crc << 1) ^ 0x1021)
            else:
                self._crc = self._crc << 1
        return

    def _sendcommand(self,address,command):
        self.crc_clear()
        self.crc_update(address)
        self._port.write(address.to_bytes(1, 'big'))
        self.crc_update(command)
        self._port.write(command.to_bytes(1, 'big'))
        return

    def _readchecksumword(self):
        data = self._port.read(2)
        if len(data)==2:
            crc = (data[0]<<8) | data[1]
            return (1,crc)  
        return (0,0)
        
    def _readbyte(self):
        data = self._port.read(1)
        if len(data):
            val = ord(data)
            self.crc_update(val)
            return (1,val)  
        return (0,0)

    def writebytes(self,vals):
        self._port.write(vals)
            
    def _writebyte(self,val):
        self.crc_update(val&0xFF)
        self._port.write(val.to_bytes(1, 'big'))

    def _writeword(self,val):
        self._writebyte((val>>8)&0xFF)
        self._writebyte(val&0xFF)
		
    def _writechecksum(self):
        self._writeword(self._crc&0xFFFF)
        val = self._readbyte()
        if(len(val)>0):
            if val[0]:
                return True
        return False

    #User accessible functions
    def WriteBootFlag(self):
        trys=self._trystimeout
        while trys:
            self._sendcommand(0x80,0xFF)
            self._writebyte(0xFF)
            self._writebyte(0xA5)
            self._writebyte(0xA5)
            self._writebyte(0x12)
            self._writebyte(0x34)
            if self._writechecksum():
                self._port.close()
                return True
            trys=trys-1
        return False
        
    def Open(self):
        try:
            self._port = serial.Serial(port=self.comport, baudrate=self.rate, timeout=1, interCharTimeout=self.timeout)
        except:
            return 0
        return 1

if(len(sys.argv)==1):
    exit();

#Windows comport name: COM1, COM2, COM3, COM4 etc...
#Linux comport name: /dev/ttyACM0, /dev/ttyACM1 etc...
dmg = Utilities(sys.argv[1],115200)

print("Starting Bootloader",end=" ")
dmg.Open()
time.sleep(1)
if(dmg.WriteBootFlag()):
    time.sleep(2);
    dmg.Open()

dmg._writebyte(0x01)
retval = dmg._readbyte()
if(retval[0]==0 or (retval[0]==1 and retval[1]!=0xAA)):
    print("Error: Bootloader Start:", retval)
    exit()
    
with open("PIBOYDMG32K.bin", "rb") as binaryfile:
    while(True):
        firmware = bytearray(binaryfile.read(256))
        if(len(firmware)!=0):
            dmg.writebytes(firmware)
            retval = dmg._readbyte()
            if(retval[0]==0 or (retval[0]==1 and retval[1]!=0xAA)):
                print("Error: Firmware Write:", len(firmware), retval)
                exit()
            print(".",end="",flush=True)
        else:
            print("Done");
            break

#reset
time.sleep(1)
dmg._writebyte(0x04)
